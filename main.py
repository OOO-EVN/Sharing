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
from aiogram.dispatcher.filters import BoundFilter
from aiogram import F

from aiogram.utils import executor

from dotenv import load_dotenv
from openpyxl import Workbook
from openpyxl.styles import Font, Alignment

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

load_dotenv()

BOT_TOKEN = os.getenv('BOT_TOKEN')
if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN не найден в .env файле. Пожалуйста, добавьте его.")

ADMIN_IDS = [int(admin_id) for admin_id in os.getenv('ADMIN_IDS', '').split(',') if admin_id.strip()]
if not ADMIN_IDS:
    pass

ALLOWED_CHAT_IDS = [int(chat_id) for chat_id in os.getenv('ALLOWED_CHAT_IDS', '').split(',') if chat_id.strip()]
if not ALLOWED_CHAT_IDS:
    pass

DB_NAME = 'scooters.db'
TIMEZONE = pytz.timezone('Asia/Almaty') 

YANDEX_SCOOTER_PATTERN = re.compile(r'\b\d{8}\b')
WOOSH_SCOOTER_PATTERN = re.compile(r'\b[A-Z]{2}\d{4}\b', re.IGNORECASE) 
JET_SCOOTER_PATTERN = re.compile(r'\b\d{3}-?\d{3}\b') 

# Новый паттерн для распознавания формата "Сервис Количество" (например, "Whoosh 19")
BATCH_QUANTITY_PATTERN = re.compile(r'\b(whoosh|jet|yandex)\s+(\d+)\b', re.IGNORECASE)
SERVICE_MAP = {"yandex": "Яндекс", "whoosh": "Whoosh", "jet": "Jet"}

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(bot) 

db_executor = ThreadPoolExecutor(max_workers=4)

class IsAdminFilter(BoundFilter):
    key = 'is_admin'
    def __init__(self, is_admin):
        self.is_admin = is_admin
    async def check(self, message: types.Message):
        return message.from_user.id in ADMIN_IDS

class IsAllowedChatFilter(BoundFilter):
    key = 'is_allowed_chat'
    def __init__(self, is_allowed_chat):
        self.is_allowed_chat = is_allowed_chat
    async def check(self, message: types.Message):
        if message.chat.type == types.ChatType.PRIVATE and message.from_user.id in ADMIN_IDS:
            return True
        if message.chat.type in [types.ChatType.GROUP, types.ChatType.SUPERGROUP] and message.chat.id in ALLOWED_CHAT_IDS:
            return True
        return False

dp.filters_factory.bind(IsAdminFilter)
dp.filters_factory.bind(IsAllowedChatFilter)

def init_db_sync():
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("PRAGMA journal_mode=WAL;")
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS accepted_scooters (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            scooter_number TEXT NOT NULL,
            service TEXT NOT NULL,
            accepted_by_user_id INTEGER NOT NULL,
            accepted_by_username TEXT,
            accepted_by_fullname TEXT NOT NULL,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
            chat_id INTEGER
        )
    ''')
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_timestamp ON accepted_scooters (timestamp);")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_user_service ON accepted_scooters (accepted_by_user_id, service);")
    conn.commit()
    conn.close()

def insert_scooter_record_sync(scooter_number, service, user_id, username, fullname, chat_id):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    now_localized = datetime.datetime.now(TIMEZONE)
    timestamp_str = now_localized.strftime("%Y-%m-%d %H:%M:%S")
    try:
        cursor.execute('''
            INSERT INTO accepted_scooters (scooter_number, service, accepted_by_user_id, accepted_by_username, accepted_by_fullname, timestamp, chat_id)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        ''', (scooter_number, service, user_id, username, fullname, timestamp_str, chat_id))
        conn.commit()
    except sqlite3.Error as e:
        pass
    finally:
        conn.close()

def insert_batch_scooter_records_sync(records_data):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    try:
        cursor.executemany('''
            INSERT INTO accepted_scooters (scooter_number, service, accepted_by_user_id, accepted_by_username, accepted_by_fullname, timestamp, chat_id)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        ''', records_data)
        conn.commit()
    except sqlite3.Error as e:
        pass
    finally:
        conn.close()

def get_scooter_records_sync(date_filter=None):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    try:
        if date_filter == 'today':
            today_localized = datetime.datetime.now(TIMEZONE).strftime("%Y-%m-%d")
            cursor.execute("SELECT id, scooter_number, service, accepted_by_user_id, accepted_by_username, accepted_by_fullname, timestamp, chat_id FROM accepted_scooters WHERE DATE(timestamp) = ?", (today_localized,))
        else:
            cursor.execute("SELECT id, scooter_number, service, accepted_by_user_id, accepted_by_username, accepted_by_fullname, timestamp, chat_id FROM accepted_scooters")
        records = cursor.fetchall()
        return records
    except sqlite3.Error as e:
        return []
    finally:
        conn.close()

async def async_run_db_op(func, *args):
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(db_executor, func, *args)

async def async_init_db():
    await async_run_db_op(init_db_sync)

async def async_insert_scooter_record(scooter_number, service, user_id, username, fullname, chat_id):
    await async_run_db_op(insert_scooter_record_sync, scooter_number, service, user_id, username, fullname, chat_id)

async def async_insert_batch_scooter_records(records_data):
    await async_run_db_op(insert_batch_scooter_records_sync, records_data)

async def async_get_scooter_records(date_filter=None):
    return await async_run_db_op(get_scooter_records_sync, date_filter)

@dp.message_handler(commands=['start'], is_allowed_chat=True)
async def command_start_handler(message: types.Message) -> None:
    allowed_chats_info = ', '.join(map(str, ALLOWED_CHAT_IDS)) if ALLOWED_CHAT_IDS else "не указаны"
    response = (f"Привет, {message.from_user.full_name}! Я бот для приёма самокатов.\n\n"
                f"Я работаю в группе с ID: `{allowed_chats_info}` и в личных сообщениях с администраторами.\n"
                f"Твой ID чата: `{message.chat.id}`")
    await message.answer(response, parse_mode=types.ParseMode.MARKDOWN)

@dp.message_handler(commands=['batch_accept'], is_admin=True)
@dp.message_handler(commands=['batch_accept'], is_allowed_chat=True)
async def batch_accept_handler(message: types.Message) -> None:
    args = message.get_args().split()
    if len(args) != 2:
        await message.reply("Используйте: `/batch_accept <сервис> <количество>`\nПример: `/batch_accept Yandex 20`", parse_mode=types.ParseMode.MARKDOWN)
        return

    service_raw, quantity_str = args
    service = SERVICE_MAP.get(service_raw.lower())

    if not service:
        await message.reply("Неизвестный сервис. Доступны: `Yandex`, `Whoosh`, `Jet`.", parse_mode=types.ParseMode.MARKDOWN)
        return
    try:
        quantity = int(quantity_str)
        if quantity <= 0: raise ValueError
    except ValueError:
        await message.reply("Количество должно быть положительным числом.")
        return

    user_id = message.from_user.id
    username = message.from_user.username
    fullname = message.from_user.full_name
    chat_id = message.chat.id
    timestamp_now_str = datetime.datetime.now(TIMEZONE).strftime("%Y-%m-%d %H:%M:%S")

    records_to_insert = []
    for i in range(quantity):
        placeholder_number = f"{service.upper()}_BATCH_{timestamp_now_str.replace(' ', '_').replace(':', '')}_{i+1}"
        records_to_insert.append((placeholder_number, service, user_id, username, fullname, timestamp_now_str, chat_id))
    
    await async_insert_batch_scooter_records(records_to_insert)
    
    user_mention = f"@{username}" if username else f"<a href='tg://user?id={user_id}'>{fullname}</a>"
    await message.reply(f"{user_mention}, принято {quantity} самокатов сервиса {service}.", parse_mode=types.ParseMode.HTML)

@dp.message_handler(commands=['today_stats', 'export_today_excel', 'export_all_excel'], is_admin=True)
async def admin_commands_handler(message: types.Message) -> None:
    command = message.get_command(pure=True)
    date_filter = None
    if command == 'today_stats':
        date_filter = 'today'
    elif command in ['export_today_excel', 'export_all_excel']:
        date_filter = 'today' if 'today' in command else None

    if command == 'today_stats':
        records = await async_get_scooter_records(date_filter='today')
        if not records:
            await message.answer("Сегодня пока ничего не принято.")
            return

        users_stats = {}
        for record in records:
            service = record[2]
            user_id = record[3]
            username = record[4]
            fullname = record[5]

            if user_id not in users_stats:
                users_stats[user_id] = {'display_name': f"@{username}" if username else fullname, 'services': {}}
            users_stats[user_id]['services'][service] = users_stats[user_id]['services'].get(service, 0) + 1
        
        response_parts = ["Статистика за сегодня:"]
        total_all_users = 0
        for user_id, user_data in users_stats.items():
            user_total = sum(user_data['services'].values())
            total_all_users += user_total
            response_parts.append(f"\n<b>{user_data['display_name']}</b> - всего: {user_total} шт.")
            for service, count in user_data['services'].items():
                response_parts.append(f"  - {service}: {count} шт.")
        
        response_parts.append(f"\n<b>Общий итог за сегодня: {total_all_users} шт.</b>")
        await message.answer("\n".join(response_parts), parse_mode=types.ParseMode.HTML)
    
    else:
        await message.answer(f"Формирую отчет{' за сегодня' if date_filter == 'today' else ' за все время'}...")
        records = await async_get_scooter_records(date_filter=date_filter)
        if not records:
            await message.answer("Нет данных для экспорта.")
            return
        
        report_type = "today" if date_filter == "today" else "full"
        excel_file = create_excel_report(records, f"Отчет {report_type}")
        filename = f"report_{report_type}_{datetime.date.today().isoformat()}.xlsx"
        await bot.send_document(message.chat.id, types.InputFile(excel_file, filename=filename), caption="Ваш отчет готов.")


def create_excel_report(records, sheet_name):
    wb = Workbook()
    ws = wb.active
    ws.title = sheet_name
    
    headers = ["ID", "Номер Самоката", "Сервис", "ID Пользователя", "Ник", "Полное имя", "Время Принятия", "ID Чата"]
    ws.append(headers)
    header_font = Font(bold=True)
    for cell in ws[1]:
        cell.font = header_font
    
    for row in records:
        ws.append(row)

    for col in ws.columns:
        max_length = 0
        column = col[0].column_letter
        for cell in col:
            try:
                if cell.value:
                    max_length = max(max_length, len(str(cell.value)))
            except TypeError:
                pass
        adjusted_width = (max_length + 2)
        ws.column_dimensions[column].width = adjusted_width

    ws_summary = wb.create_sheet("Сводка")
    summary_headers = ["Пользователь", "Сервис", "Количество"]
    ws_summary.append(summary_headers)
    for cell in ws_summary[1]:
        cell.font = header_font

    user_service_counts = {}
    for record in records:
        service = record[2]
        user_fullname = record[5]
        if user_fullname not in user_service_counts:
            user_service_counts[user_fullname] = {}
        user_service_counts[user_fullname][service] = user_service_counts[user_fullname].get(service, 0) + 1

    for user, services in user_service_counts.items():
        for service, count in services.items():
            ws_summary.append([user, service, count])
    
    for col in ws_summary.columns:
        max_length = 0
        column = col[0].column_letter
        for cell in col:
            try:
                if cell.value:
                    max_length = max(max_length, len(str(cell.value)))
            except TypeError:
                pass
        adjusted_width = (max_length + 2)
        ws_summary.column_dimensions[column].width = adjusted_width

    buffer = BytesIO()
    wb.save(buffer)
    buffer.seek(0)
    return buffer

@dp.message_handler(
    lambda message: not message.is_command(),
    content_types=types.ContentType.ANY, 
    is_allowed_chat=True
)
async def handle_scooter_numbers(message: types.Message) -> None:
    text_to_check = message.text or message.caption
    if not text_to_check:
        return

    user_id = message.from_user.id
    username = message.from_user.username
    fullname = message.from_user.full_name
    chat_id = message.chat.id
    timestamp_now_str = datetime.datetime.now(TIMEZONE).strftime("%Y-%m-%d %H:%M:%S")

    # 1. Попытка распознать формат "Сервис Количество"
    batch_matches = BATCH_QUANTITY_PATTERN.findall(text_to_check)
    
    if batch_matches:
        records_to_insert = []
        accepted_by_service = defaultdict(int)

        for service_raw, quantity_str in batch_matches:
            service = SERVICE_MAP.get(service_raw.lower())
            try:
                quantity = int(quantity_str)
                if quantity > 0:
                    for i in range(quantity):
                        # Создаем заглушку для номера самоката
                        placeholder_number = f"{service.upper()}_BATCH_{timestamp_now_str.replace(' ', '_').replace(':', '')}_{i+1}"
                        records_to_insert.append((placeholder_number, service, user_id, username, fullname, timestamp_now_str, chat_id))
                    accepted_by_service[service] += quantity
            except ValueError:
                continue # Пропускаем, если количество не число

        if records_to_insert:
            await async_insert_batch_scooter_records(records_to_insert)
            
            response_parts = []
            user_mention = f"@{username}" if username else f"<a href='tg://user?id={user_id}'>{fullname}</a>"
            total_accepted = sum(accepted_by_service.values())
            response_parts.append(f"{user_mention}, принято от тебя {total_accepted} шт.:")
            for service, count in accepted_by_service.items():
                if count > 0:
                    response_parts.append(f"{service}: {count}")
            await message.reply("\n".join(response_parts), parse_mode=types.ParseMode.HTML)
            return # Завершаем обработку, если успешно обработали пакетный ввод
    
    # 2. Если не найдено пакетных записей, пытаемся найти конкретные номера самокатов
    
    accepted_by_service = {"Яндекс": 0, "Whoosh": 0, "Jet": 0}
    
    patterns = {
        "Яндекс": YANDEX_SCOOTER_PATTERN,
        "Whoosh": WOOSH_SCOOTER_PATTERN,
        "Jet": JET_SCOOTER_PATTERN
    }

    records_to_insert = []

    for service, pattern in patterns.items():
        numbers = pattern.findall(text_to_check)
        for num in numbers:
            records_to_insert.append((num, service, user_id, username, fullname, timestamp_now_str, chat_id))
            accepted_by_service[service] += 1
    
    if records_to_insert:
        await async_insert_batch_scooter_records(records_to_insert)

    total_accepted = sum(accepted_by_service.values())
    if total_accepted > 0:
        response_parts = []
        user_mention = f"@{username}" if username else f"<a href='tg://user?id={user_id}'>{fullname}</a>"
        response_parts.append(f"{user_mention}, принято от тебя {total_accepted} шт.:")
        for service, count in accepted_by_service.items():
            if count > 0:
                response_parts.append(f"{service}: {count}")
        await message.reply("\n".join(response_parts), parse_mode=types.ParseMode.HTML)
    else:
        pass

@dp.message_handler(content_types=types.ContentType.ANY)
async def handle_unallowed_messages(message: types.Message) -> None:
    pass

async def on_startup(dp):
    await async_init_db()

async def on_shutdown(dp):
    if db_executor:
        db_executor.shutdown(wait=True)

if __name__ == "__main__":
    executor.start_polling(dp, skip_updates=True, on_startup=on_startup, on_shutdown=on_shutdown)
