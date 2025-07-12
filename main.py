import asyncio
import re
import os
import sqlite3
import datetime
from io import BytesIO
import pytz
import logging
from concurrent.futures import ThreadPoolExecutor
from collections import defaultdict
from typing import List, Tuple

# Изменения для aiogram 2.x
from aiogram import Bot, Dispatcher, types
from aiogram.contrib.fsm_storage.memory import MemoryStorage
from aiogram.utils import executor
from aiogram.dispatcher.filters import BoundFilter # Добавляем для создания классов фильтров

from dotenv import load_dotenv
from openpyxl import Workbook
from openpyxl.styles import Font

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

load_dotenv()

BOT_TOKEN = os.getenv('BOT_TOKEN')
if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN не найден в .env файле. Пожалуйста, добавьте его.")

try:
    ADMIN_IDS = {int(admin_id) for admin_id in os.getenv('ADMIN_IDS', '').split(',') if admin_id.strip()}
    ALLOWED_CHAT_IDS = {int(chat_id) for chat_id in os.getenv('ALLOWED_CHAT_IDS', '').split(',') if chat_id.strip()}
except ValueError:
    logging.error("Не удалось прочитать ADMIN_IDS или ALLOWED_CHAT_IDS. Убедитесь, что они являются числами, разделенными запятыми.")
    ADMIN_IDS = set()
    ALLOWED_CHAT_IDS = set()

DB_NAME = 'scooters.db'
TIMEZONE = pytz.timezone('Asia/Almaty')

YANDEX_SCOOTER_PATTERN = re.compile(r'\b(\d{8})\b')
WOOSH_SCOOTER_PATTERN = re.compile(r'\b([A-ZА-Я]{2}\d{4})\b', re.IGNORECASE)
JET_SCOOTER_PATTERN = re.compile(r'\b(\d{3}-?\d{3})\b')

BATCH_QUANTITY_PATTERN = re.compile(r'\b(whoosh|jet|yandex|вуш|джет|яндекс|w|j|y)\s+(\d+)\b', re.IGNORECASE)
SERVICE_ALIASES = {
    "yandex": "Яндекс", "яндекс": "Яндекс", "y": "Яндекс",
    "whoosh": "Whoosh", "вуш": "Whoosh", "w": "Whoosh",
    "jet": "Jet", "джет": "Jet", "j": "Jet"
}
SERVICE_MAP = {"yandex": "Яндекс", "whoosh": "Whoosh", "jet": "Jet"}

bot = Bot(token=BOT_TOKEN, parse_mode="HTML")
storage = MemoryStorage()
dp = Dispatcher(bot, storage=storage)

# **ИСПРАВЛЕННЫЙ МЕТОД ФИЛЬТРОВ ДЛЯ AIOGRAM 2.X**
# Создаем классы фильтров, наследующиеся от BoundFilter
class IsAdminFilter(BoundFilter):
    async def check(self, message: types.Message) -> bool:
        return message.from_user.id in ADMIN_IDS

class IsAllowedChatFilter(BoundFilter):
    async def check(self, message: types.Message) -> bool:
        if message.chat.type == 'private' and message.from_user.id in ADMIN_IDS:
            return True
        if message.chat.type in ['group', 'supergroup'] and message.chat.id in ALLOWED_CHAT_IDS:
            return True
        logging.warning(f"Сообщение от {message.from_user.id} в чате {message.chat.id} было заблокировано фильтром.")
        return False

def run_db_query(query: str, params: tuple = (), fetch: str = None):
    conn = None
    try:
        conn = sqlite3.connect(DB_NAME, timeout=10)
        conn.execute("PRAGMA journal_mode=WAL;")
        cursor = conn.cursor()
        cursor.execute(query, params)
        conn.commit()
        if fetch == 'one':
            return cursor.fetchone()
        if fetch == 'all':
            return cursor.fetchall()
        return cursor.lastrowid
    except sqlite3.Error as e:
        logging.error(f"Ошибка базы данных: {e}\nЗапрос: {query}")
        return None
    finally:
        if conn:
            conn.close()

def init_db():
    run_db_query('''
        CREATE TABLE IF NOT EXISTS accepted_scooters (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            scooter_number TEXT NOT NULL,
            service TEXT NOT NULL,
            accepted_by_user_id INTEGER NOT NULL,
            accepted_by_username TEXT,
            accepted_by_fullname TEXT NOT NULL,
            timestamp DATETIME NOT NULL,
            chat_id INTEGER NOT NULL
        )
    ''')
    run_db_query("CREATE INDEX IF NOT EXISTS idx_timestamp ON accepted_scooters (timestamp);")
    run_db_query("CREATE INDEX IF NOT EXISTS idx_user_service ON accepted_scooters (accepted_by_user_id, service);")
    logging.info("База данных успешно инициализирована.")

def insert_batch_records(records_data: List[Tuple]):
    conn = None
    try:
        conn = sqlite3.connect(DB_NAME, timeout=10)
        conn.execute("PRAGMA journal_mode=WAL;")
        cursor = conn.cursor()
        cursor.executemany('''
            INSERT INTO accepted_scooters (scooter_number, service, accepted_by_user_id, accepted_by_username, accepted_by_fullname, timestamp, chat_id)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        ''', records_data)
        conn.commit()
    except sqlite3.Error as e:
        logging.error(f"Ошибка пакетной вставки в БД: {e}")
    finally:
        if conn:
            conn.close()

async def db_write_batch(records_data: List[Tuple]):
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(db_executor, insert_batch_records, records_data)

async def db_fetch_all(query: str, params: tuple = ()):
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(db_executor, run_db_query, query, params, 'all')

# Регистрация обработчиков
@dp.message_handler(commands="start", IsAllowedChatFilter()) # Передаем экземпляр фильтра
async def command_start_handler(message: types.Message):
    allowed_chats_info = ', '.join(map(str, ALLOWED_CHAT_IDS)) if ALLOWED_CHAT_IDS else "не указаны"
    response = (
        f"Привет, {message.from_user.full_name}! Я бот для приёма самокатов.\n\n"
        f"Просто отправь мне номер самоката, и я его учту.\n"
        f"Для пакетного приёма используй формат: `сервис количество` (например, `Яндекс 10`, `y 5`, `Whoosh 15`, `w 20`, `Jet 8`, `j 3`).\n\n"
        f"Я работаю в группах с ID: `{allowed_chats_info}` и в личных сообщениях с администраторами.\n"
        f"Твой ID чата: `{message.chat.id}`"
    )
    await message.answer(response, parse_mode="Markdown")

@dp.message_handler(commands="batch_accept", IsAllowedChatFilter())
async def batch_accept_handler(message: types.Message):
    args = message.get_args().split()
    if len(args) != 2:
        await message.reply("Используйте: `/batch_accept <сервис> <количество>`\nПример: `/batch_accept Yandex 20` или `/batch_accept y 20`", parse_mode="Markdown")
        return

    service_raw, quantity_str = args
    service = SERVICE_ALIASES.get(service_raw.lower())

    if not service:
        await message.reply("Неизвестный сервис. Доступны: `Yandex` (`y`), `Whoosh` (`w`), `Jet` (`j`).", parse_mode="Markdown")
        return
    try:
        quantity = int(quantity_str)
        if not (0 < quantity <= 200):
            raise ValueError
    except ValueError:
        await message.reply("Количество должно быть числом от 1 до 200.")
        return

    user = message.from_user
    now_localized_str = datetime.datetime.now(TIMEZONE).strftime("%Y-%m-%d %H:%M:%S")

    records_to_insert = [
        (
            f"{service.upper()}_BATCH_{i+1}",
            service, user.id, user.username, user.full_name, now_localized_str, message.chat.id
        ) for i in range(quantity)
    ]

    await db_write_batch(records_to_insert)

    user_mention = types.User.get_mention(user)
    await message.reply(f"{user_mention}, принято {quantity} самокатов сервиса <b>{service}</b>.")

@dp.message_handler(commands="today_stats", IsAdminFilter()) # Передаем экземпляр фильтра
async def today_stats_handler(message: types.Message):
    today_str = datetime.datetime.now(TIMEZONE).strftime("%Y-%m-%d")
    query = "SELECT service, accepted_by_user_id, accepted_by_username, accepted_by_fullname FROM accepted_scooters WHERE DATE(timestamp) = ?"
    records = await db_fetch_all(query, (today_str,))

    if not records:
        await message.answer("Сегодня пока ничего не принято.")
        return

    user_stats = defaultdict(lambda: defaultdict(int))
    user_info = {}

    for service, user_id, username, fullname in records:
        user_stats[user_id][service] += 1
        if user_id not in user_info:
            user_info[user_id] = f"@{username}" if username else fullname

    response_parts = ["<b>Статистика за сегодня:</b>"]
    total_all_users = 0

    for user_id, services in user_stats.items():
        user_total = sum(services.values())
        total_all_users += user_total
        response_parts.append(f"\n<b>{user_info[user_id]}</b> - всего: {user_total} шт.")
        for service, count in sorted(services.items()):
            response_parts.append(f"  - {service}: {count} шт.")

    response_parts.append(f"\n<b>Общий итог за сегодня: {total_all_users} шт.</b>")
    await message.answer("\n".join(response_parts))

@dp.message_handler(commands=["export_today_excel", "export_all_excel"], IsAdminFilter()) # Передаем экземпляр фильтра
async def export_excel_handler(message: types.Message):
    is_today = message.get_command() == '/export_today_excel'
    date_filter = ' за сегодня' if is_today else ' за все время'
    await message.answer(f"Формирую отчет{date_filter}...")

    query = "SELECT id, scooter_number, service, accepted_by_user_id, accepted_by_username, accepted_by_fullname, timestamp, chat_id FROM accepted_scooters"
    if is_today:
        today_str = datetime.datetime.now(TIMEZONE).strftime("%Y-%m-%d")
        query += " WHERE DATE(timestamp) = ?"
        records = await db_fetch_all(query, (today_str,))
    else:
        query += " ORDER BY timestamp DESC"
        records = await db_fetch_all(query)

    if not records:
        await message.answer("Нет данных для экспорта.")
        logging.info(f"Нет данных для экспорта отчета {date_filter}.")
        return

    try:
        excel_file = create_excel_report(records)
        report_type = "today" if is_today else "full"
        filename = f"report_{report_type}_{datetime.date.today().isoformat()}.xlsx"
        
        logging.info(f"Попытка отправить Excel файл: {filename}, размер: {excel_file.getbuffer().nbytes} байт.")
        # В aiogram 2.x types.InputFile работает с BytesIO напрямую
        await bot.send_document(message.chat.id, types.InputFile(excel_file, filename=filename), caption="Ваш отчет готов.")
        logging.info(f"Excel файл {filename} успешно отправлен.")
    except Exception as e:
        logging.error(f"Ошибка при отправке Excel файла: {e}", exc_info=True)
        await message.answer("Произошла ошибка при отправке отчета. Пожалуйста, свяжитесь с администратором.")

def create_excel_report(records: List[Tuple]) -> BytesIO:
    wb = Workbook()
    ws = wb.active
    ws.title = "Данные"

    headers = ["ID", "Номер Самоката", "Сервис", "ID Пользователя", "Ник", "Полное имя", "Время Принятия", "ID Чата"]
    ws.append(headers)
    header_font = Font(bold=True)
    for cell in ws[1]:
        cell.font = header_font

    for row in records:
        ws.append(row)

    for col in ws.columns:
        max_length = 0
        column_letter = col[0].column_letter
        for cell in col:
            try:
                if cell.value:
                    max_length = max(max_length, len(str(cell.value)))
            except:
                pass
        adjusted_width = (max_length + 2) * 1.2
        ws.column_dimensions[column_letter].width = adjusted_width

    ws_summary = wb.create_sheet("Сводка")
    summary_headers = ["Пользователь", "Сервис", "Количество"]
    ws_summary.append(summary_headers)
    for cell in ws_summary[1]:
        cell.font = header_font

    user_service_counts = defaultdict(lambda: defaultdict(int))
    for record in records:
        service = record[2]
        user_fullname = record[5]
        user_service_counts[user_fullname][service] += 1

    for user, services in sorted(user_service_counts.items()):
        for service, count in sorted(services.items()):
            ws_summary.append([user, service, count])
    
    for col in ws_summary.columns:
        max_length = 0
        column_letter = col[0].column_letter
        for cell in col:
            try:
                if cell.value:
                    max_length = max(max_length, len(str(cell.value)))
            except:
                pass
        adjusted_width = (max_length + 2) * 1.2
        ws_summary.column_dimensions[column_letter].width = adjusted_width

    buffer = BytesIO()
    wb.save(buffer)
    buffer.seek(0)
    return buffer

@dp.message_handler(content_types=types.ContentTypes.TEXT, IsAllowedChatFilter())
async def handle_scooter_numbers(message: types.Message):
    text = message.text
    if not text:
        return

    user = message.from_user
    now_localized_str = datetime.datetime.now(TIMEZONE).strftime("%Y-%m-%d %H:%M:%S")

    records_to_insert = []
    accepted_summary = defaultdict(int)
    
    text_for_numbers = text

    batch_matches = BATCH_QUANTITY_PATTERN.findall(text)
    if batch_matches:
        for service_raw, quantity_str in batch_matches:
            service = SERVICE_ALIASES.get(service_raw.lower())
            try:
                quantity = int(quantity_str)
                if service and 0 < quantity <= 200:
                    for i in range(quantity):
                        placeholder_number = f"{service.upper()}_BATCH_{i+1}"
                        records_to_insert.append((placeholder_number, service, user.id, user.username, user.full_name, now_localized_str, message.chat.id))
                    accepted_summary[service] += quantity
            except (ValueError, TypeError):
                continue
        text_for_numbers = BATCH_QUANTITY_PATTERN.sub('', text)

    patterns = {
        "Яндекс": YANDEX_SCOOTER_PATTERN,
        "Whoosh": WOOSH_SCOOTER_PATTERN,
        "Jet": JET_SCOOTER_PATTERN
    }
    
    processed_numbers = set()

    for service, pattern in patterns.items():
        numbers = pattern.findall(text_for_numbers)
        for num in numbers:
            clean_num = num.replace('-', '') if service == "Jet" else num.upper()
            
            if clean_num in processed_numbers:
                continue
            
            records_to_insert.append((clean_num, service, user.id, user.username, user.full_name, now_localized_str, message.chat.id))
            accepted_summary[service] += 1
            processed_numbers.add(clean_num)

    if not records_to_insert:
        return

    await db_write_batch(records_to_insert)

    response_parts = []
    user_mention = types.User.get_mention(user)
    total_accepted = sum(accepted_summary.values())
    response_parts.append(f"{user_mention}, принято {total_accepted} шт.:")

    for service, count in sorted(accepted_summary.items()):
        if count > 0:
            response_parts.append(f"  - <b>{service}</b>: {count} шт.")

    await message.reply("\n".join(response_parts))

async def on_startup(dispatcher: Dispatcher):
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(db_executor, init_db)
    
    admin_commands = [
        types.BotCommand(command="start", description="Начало работы"),
        types.BotCommand(command="today_stats", description="Статистика за сегодня"),
        types.BotCommand(command="export_today_excel", description="Экспорт Excel за сегодня"),
        types.BotCommand(command="export_all_excel", description="Экспорт Excel за все время"),
        types.BotCommand(command="batch_accept", description="Пакетный прием (сервис кол-во)"),
    ]
    await dispatcher.bot.set_my_commands(admin_commands)
    logging.info("Бот запущен и команды установлены.")


async def on_shutdown(dispatcher: Dispatcher):
    if db_executor:
        db_executor.shutdown(wait=True)
    logging.info("Пул потоков БД остановлен.")
    logging.info("Бот остановлен.")

if __name__ == "__main__":
    # В aiogram 2.x фильтры, наследующие от BoundFilter, можно передавать прямо в декоратор
    # Нет необходимости в dp.filters_factory.bind() для этого способа.
    executor.start_polling(dp, on_startup=on_startup, on_shutdown=on_shutdown, skip_updates=True)
