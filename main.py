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

from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command, CommandObject, BaseFilter
from aiogram.types import Message
from aiogram import F
from aiogram.enums import ParseMode # Импортируем ParseMode
from dotenv import load_dotenv # Убедитесь, что это импортировано
from openpyxl import Workbook
from openpyxl.styles import Font
from typing import List, Tuple # Убедитесь, что это импортировано

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

load_dotenv() # Здесь теперь не будет NameError

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

# Инициализация бота с default_parse_mode
bot = Bot(token=BOT_TOKEN, default_parse_mode=ParseMode.HTML) # Исправлено

dp = Dispatcher()
db_executor = ThreadPoolExecutor(max_workers=5)

class IsAdminFilter(BaseFilter):
    async def __call__(self, message: Message) -> bool:
        return message.from_user.id in ADMIN_IDS

class IsAllowedChatFilter(BaseFilter):
    async def __call__(self, message: Message) -> bool:
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

def insert_batch_records(records_data: List[Tuple]): # Исправлено: List[Tuple]
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

async def db_write_batch(records_data: List[Tuple]): # Исправлено: List[Tuple]
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(db_executor, insert_batch_records, records_data)

async def db_fetch_all(query: str, params: tuple = ()):
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(db_executor, run_db_query, query, params, 'all')

@dp.message(Command("start"), IsAllowedChatFilter())
async def command_start_handler(message: Message):
    allowed_chats_info = ', '.join(map(str, ALLOWED_CHAT_IDS)) if ALLOWED_CHAT_IDS else "не указаны"
    response = (
        f"Привет, {message.from_user.full_name}! Я бот для приёма самокатов.\n\n"
        f"Просто отправь мне номер самоката, и я его учту.\n"
        f"Для пакетного приёма используй формат: `сервис количество` (например, `Яндекс 10`).\n\n"
        f"Я работаю в группах с ID: `{allowed_chats_info}` и в личных сообщениях с администраторами.\n"
        f"Твой ID чата: `{message.chat.id}`"
    )
    await message.answer(response, parse_mode="Markdown")

@dp.message(Command("batch_accept"), IsAllowedChatFilter())
async def batch_accept_handler(message: Message, command: CommandObject):
    if command.args is None:
        await message.reply("Используйте: `/batch_accept <сервис> <количество>`\nПример: `/batch_accept Yandex 20`", parse_mode="Markdown")
        return

    args = command.args.split()
    if len(args) != 2:
        await message.reply("Неверный формат. Используйте: `/batch_accept <сервис> <количество>`", parse_mode="Markdown")
        return

    service_raw, quantity_str = args
    service = SERVICE_ALIASES.get(service_raw.lower())

    if not service:
        await message.reply("Неизвестный сервис. Доступны: `Yandex`, `Whoosh`, `Jet` (или их сокращения: `y`, `w`, `j`).", parse_mode="Markdown")
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

    user_mention = user.mention_html()
    await message.reply(f"{user_mention}, принято {quantity} самокатов сервиса <b>{service}</b>.")

@dp.message(Command("today_stats"), IsAdminFilter())
async def today_stats_handler(message: Message):
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

@dp.message(Command("export_today_excel", "export_all_excel"), IsAdminFilter())
async def export_excel_handler(message: Message, command: CommandObject):
    is_today = command.command == 'export_today_excel'
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
        return

    excel_file = create_excel_report(records)
    report_type = "today" if is_today else "full"
    filename = f"report_{report_type}_{datetime.date.today().isoformat()}.xlsx"
    await bot.send_document(message.chat.id, types.InputFile(excel_file, filename=filename), caption="Ваш отчет готов.")

def create_excel_report(records: List[Tuple]) -> BytesIO: # Исправлено: List[Tuple]
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

@dp.message((F.text | F.caption), IsAllowedChatFilter())
async def handle_scooter_numbers(message: Message):
    text = message.text or message.caption
    if not text:
        return

    user = message.from_user
    now_localized_str = datetime.datetime.now(TIMEZONE).strftime("%Y-%m-%d %H:%M:%S")

    records_to_insert = []
    accepted_summary = defaultdict(int)
    
    # Сначала найдем и обработаем все пакетные совпадения
    batch_matches = BATCH_QUANTITY_PATTERN.findall(text)
    
    # Создаем временную копию текста для удаления пакетных команд
    temp_text_for_numbers = text

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
                    
                    # Удаляем только что обработанную пакетную команду из temp_text_for_numbers
                    # Используем re.sub с count=1 для удаления только первого найденного совпадения
                    # Убедитесь, что `service_raw` и `quantity_str` соответствуют тому, что вы хотите удалить.
                    # Возможно, лучше удалить полное совпадение из original_text, а не только часть.
                    # Для надежности можно использовать Span из re.finditer, но это усложнит код.
                    # Текущий подход `re.sub(re.escape(f"{service_raw} {quantity_str}")` должен работать,
                    # если строка точно совпадает с паттерном.
                    temp_text_for_numbers = re.sub(re.escape(f"{service_raw} {quantity_str}"), '', temp_text_for_numbers, 1, re.IGNORECASE).strip()

            except (ValueError, TypeError):
                continue
        
    # Теперь, после обработки пакетных команд, обрабатываем оставшийся текст на наличие отдельных номеров
    text_for_individual_numbers = temp_text_for_numbers

    patterns = {
        "Яндекс": YANDEX_SCOOTER_PATTERN,
        "Whoosh": WOOSH_SCOOTER_PATTERN,
        "Jet": JET_SCOOTER_PATTERN
    }
    
    processed_numbers = set()

    for service, pattern in patterns.items():
        numbers = pattern.findall(text_for_individual_numbers) # Ищем в очищенном тексте
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
    total_accepted = sum(accepted_summary.values())
    
    if total_accepted > 0:
        for service, count in sorted(accepted_summary.items()):
            if count > 0:
                response_parts.append(f"  - <b>{service}</b>: {count} шт.")
    else:
        response_parts.append("Не удалось распознать номера самокатов или пакетные команды.")


    await message.reply("\n".join(response_parts))

async def on_startup(bot: Bot):
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(db_executor, init_db)
    admin_commands = [
        types.BotCommand(command="start", description="Начало работы"),
        types.BotCommand(command="today_stats", description="Статистика за сегодня"),
        types.BotCommand(command="export_today_excel", description="Экспорт Excel за сегодня"),
        types.BotCommand(command="export_all_excel", description="Экспорт Excel за все время"),
        types.BotCommand(command="batch_accept", description="Пакетный прием (сервис кол-во)"),
    ]
    await bot.set_my_commands(admin_commands)

async def on_shutdown():
    if db_executor:
        db_executor.shutdown(wait=True)
    logging.info("Пул потоков БД остановлен.")

async def main():
    dp.startup.register(on_startup)
    dp.shutdown.register(on_shutdown)
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
