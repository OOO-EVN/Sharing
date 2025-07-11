import asyncio
import re
import os
import sqlite3
import datetime
from io import BytesIO
import pytz

from aiogram import Bot, Dispatcher, types
from aiogram.dispatcher.filters import Command
from aiogram.dispatcher.filters import BoundFilter
from aiogram.utils import executor

from dotenv import load_dotenv
from openpyxl import Workbook
from openpyxl.styles import Font, Alignment

load_dotenv()

# --- КОНСТАНТЫ ---
BOT_TOKEN = os.getenv('BOT_TOKEN')
if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN не найден в .env файле. Пожалуйста, добавьте его.")

ADMIN_IDS = [int(admin_id) for admin_id in os.getenv('ADMIN_IDS', '').split(',') if admin_id.strip()]
if not ADMIN_IDS:
    print("Внимание: ADMIN_IDS не заданы в .env файле. Пожалуйста, добавьте ID администраторов.")

ALLOWED_CHAT_IDS = [int(chat_id) for chat_id in os.getenv('ALLOWED_CHAT_IDS', '').split(',') if chat_id.strip()]
if not ALLOWED_CHAT_IDS:
    print("Внимание: ALLOWED_CHAT_IDS не заданы в .env файле. Бот будет принимать номера только от админов.")


DB_NAME = 'scooters.db'

TIMEZONE = pytz.timezone('Asia/Almaty') 

YANDEX_SCOOTER_PATTERN = re.compile(r'\b\d{8}\b')
WOOSH_SCOOTER_PATTERN = re.compile(r'\b[A-Z]{2}\d{4}\b', re.IGNORECASE)
JET_SCOOTER_PATTERN = re.compile(r'\b\d{6}\b')
BATCH_TEXT_PATTERN = re.compile(r'(yandex|whoosh|jet)\s+(\d+)', re.IGNORECASE)

# --- ИНИЦИАЛИЗАЦИЯ БОТА И ДИСПЕТЧЕРА ---
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(bot) 

# --- КЛАССЫ ФИЛЬТРОВ ---

class IsAdminFilter(BoundFilter):
    key = 'is_admin'
    def __init__(self, is_admin):
        self.is_admin = is_admin
    async def check(self, message: types.Message):
        is_admin_check = message.from_user.id in ADMIN_IDS
        print(f"DEBUG: IsAdminFilter check for user {message.from_user.id}: {is_admin_check}")
        return is_admin_check

class IsAllowedChatFilter(BoundFilter):
    key = 'is_allowed_chat'
    def __init__(self, is_allowed_chat):
        self.is_allowed_chat = is_allowed_chat
    async def check(self, message: types.Message):
        if not self.is_allowed_chat:
            return False # Should not happen if filter is correctly used
        
        # If private chat AND sender is admin, it's allowed
        if message.chat.type == types.ChatType.PRIVATE and message.from_user.id in ADMIN_IDS:
            print(f"DEBUG: IsAllowedChatFilter check for chat {message.chat.id} (Private, Admin): True")
            return True
        
        # If group chat AND its ID is in the allowed list
        is_allowed_group = message.chat.id in ALLOWED_CHAT_IDS
        print(f"DEBUG: IsAllowedChatFilter check for chat {message.chat.id} (Group): {is_allowed_group}")
        return is_allowed_group

# Регистрация фильтров
dp.filters_factory.bind(IsAdminFilter)
dp.filters_factory.bind(IsAllowedChatFilter)


# --- ФУНКЦИИ ВЗАИМОДЕЙСТВИЯ С БАЗОЙ ДАННЫХ ---

def init_db():
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS accepted_scooters (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            scooter_number TEXT NOT NULL,
            service TEXT NOT NULL,
            accepted_by_user_id INTEGER NOT NULL,
            accepted_by_username TEXT,
            accepted_by_fullname TEXT NOT NULL,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    conn.commit()
    conn.close()

def insert_scooter_record(scooter_number, service, user_id, username, fullname):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    
    now_localized = datetime.datetime.now(TIMEZONE)
    timestamp_str = now_localized.strftime("%Y-%m-%d %H:%M:%S")

    cursor.execute('''
        INSERT INTO accepted_scooters (scooter_number, service, accepted_by_user_id, accepted_by_username, accepted_by_fullname, timestamp)
        VALUES (?, ?, ?, ?, ?, ?)
    ''', (scooter_number, service, user_id, username, fullname, timestamp_str))
    conn.commit()
    conn.close()
    print(f"DEBUG: Scooter record inserted: {scooter_number} by {fullname} ({user_id})")

def get_scooter_records(date_filter=None):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    if date_filter == 'today':
        today_localized = datetime.datetime.now(TIMEZONE).strftime("%Y-%m-%d")
        cursor.execute("SELECT * FROM accepted_scooters WHERE DATE(timestamp) = ?", (today_localized,))
    else:
        cursor.execute("SELECT * FROM accepted_scooters")
    records = cursor.fetchall()
    conn.close()
    return records

# --- ОБРАБОТЧИКИ СООБЩЕНИЙ ---

@dp.message_handler(commands=['start'])
async def command_start_handler(message: types.Message) -> None:
    print(f"DEBUG: /start command received from user {message.from_user.id} in chat {message.chat.id}")
    allowed_chats_info = ', '.join(map(str, ALLOWED_CHAT_IDS)) if ALLOWED_CHAT_IDS else "не указаны (бот принимает номера только от админов)"
    
    response = (f"Привет, {message.from_user.full_name}! Я бот для приёма самокатов.\n\n"
                f"Я принимаю номера самокатов в группах с ID: `{allowed_chats_info}`.\n"
                f"Админы могут сдавать самокаты и получать отчёты в любом чате.\n\n"
                f"Просто отправь номер самоката или фото с номером в подписи. "
                f"Для пакетной сдачи используй `/batch_accept <сервис> <количество>`.\n\n"
                f"Твой ID чата: `{message.chat.id}`"
               )
    await message.answer(response, parse_mode=types.ParseMode.MARKDOWN)

@dp.message_handler(commands=['batch_accept'], is_admin=True) # Admin can use anywhere
@dp.message_handler(commands=['batch_accept'], is_allowed_chat=True) # Non-admin can use in allowed chats
async def batch_accept_handler(message: types.Message) -> None:
    print(f"DEBUG: /batch_accept command received from user {message.from_user.id} in chat {message.chat.id}")
    args = message.get_args().split()
    if len(args) != 2:
        await message.reply("Используйте команду в формате: `/batch_accept <сервис> <количество>`\nНапример: `/batch_accept Yandex 20`", parse_mode=types.ParseMode.MARKDOWN)
        return

    service_raw = args[0].lower() 
    quantity_str = args[1]

    service_map = {
        "yandex": "Яндекс",
        "whoosh": "Whoosh",
        "jet": "Jet"
    }

    service = service_map.get(service_raw)

    if not service:
        await message.reply("Неизвестный сервис. Доступные сервисы: `Yandex`, `Whoosh`, `Jet`.", parse_mode=types.ParseMode.MARKDOWN)
        return

    try:
        quantity = int(quantity_str)
        if quantity <= 0:
            raise ValueError("Количество должно быть положительным числом.")
    except ValueError:
        await message.reply("Количество должно быть положительным числом.", parse_mode=types.ParseMode.HTML)
        return

    user_id = message.from_user.id
    username = message.from_user.username
    fullname = message.from_user.full_name

    accepted_count = 0
    timestamp_now_for_placeholder = datetime.datetime.now(TIMEZONE).strftime("%Y%m%d%H%M%S") 

    for i in range(quantity):
        placeholder_number = f"{service.upper()}_BATCH_{timestamp_now_for_placeholder}_{i+1}"
        insert_scooter_record(placeholder_number, service, user_id, username, fullname)
        accepted_count += 1

    user_mention_text = message.from_user.full_name
    if message.from_user.username:
        user_mention = f"@{message.from_user.username}"
    else:
        user_mention = f"<a href='tg://user?id={message.from_user.id}'>{user_mention_text}</a>"

    await message.reply(
        f"{user_mention}, принято {accepted_count} самокатов сервиса {service} в качестве пакетной сдачи.",
        parse_mode=types.ParseMode.HTML
    )

@dp.message_handler(commands=['today_stats', 'export_today_excel', 'export_all_excel'], is_admin=True)
async def admin_commands_handler(message: types.Message) -> None:
    print(f"DEBUG: Admin command '{message.get_command()}' received from user {message.from_user.id} in chat {message.chat.id}")
    command = message.get_command()

    if command == '/today_stats':
        records = get_scooter_records(date_filter='today')
        
        if not records:
            await message.answer("Сегодня пока ничего не принято.", parse_mode=types.ParseMode.HTML)
            return

        users_stats = {}
        for record in records:
            user_id = record[3]
            username = record[4] if record[4] else record[5]
            service = record[2]

            if user_id not in users_stats:
                users_stats[user_id] = {
                    'display_name': f"@{username}" if record[4] else record[5],
                    'services': {}
                }
            
            users_stats[user_id]['services'][service] = users_stats[user_id]['services'].get(service, 0) + 1
        
        response_parts = []
        total_all_users = 0

        for user_id, user_data in users_stats.items():
            display_name = user_data['display_name']
            services_stats = user_data['services']
            
            response_parts.append(f"{display_name} Статистика за сегодня:")
            
            user_total = 0
            for service, count in services_stats.items():
                response_parts.append(f"Принято {service}: {count}")
                user_total += count
            
            response_parts.append(f"Всего от {display_name}: {user_total} шт.")
            response_parts.append("Деп\n")
            total_all_users += user_total

        if response_parts and response_parts[-1] == "Деп\n":
            response_parts[-1] = "Деп"

        final_response = "\n".join(response_parts)
        final_response += f"\n---\nОбщий итог за сегодня: {total_all_users} шт."
        
        await message.answer(final_response, parse_mode=types.ParseMode.HTML)

    elif command == '/export_today_excel':
        await message.answer("Формирую отчет за сегодня, пожалуйста, подождите...", parse_mode=types.ParseMode.HTML)
        records = get_scooter_records(date_filter='today')
        if not records:
            await message.answer("Нет данных за сегодня для экспорта.", parse_mode=types.ParseMode.HTML)
            return

        excel_file = create_excel_report(records, "Отчет за сегодня")
        filename = f"report_today_{datetime.date.today().isoformat()}.xlsx"
        await bot.send_document(chat_id=message.chat.id, document=types.InputFile(excel_file, filename=filename))
        await message.answer("Отчет за сегодня готов.", parse_mode=types.ParseMode.HTML)

    elif command == '/export_all_excel':
        await message.answer("Формирую полный отчет, пожалуйста, подождите...", parse_mode=types.ParseMode.HTML)
        records = get_scooter_records(date_filter='all')
        if not records:
            await message.answer("Нет данных для экспорта.", parse_mode=types.ParseMode.HTML)
            return

        excel_file = create_excel_report(records, "Полный отчет")
        filename = f"full_report_{datetime.date.today().isoformat()}.xlsx"
        await bot.send_document(chat_id=message.chat.id, document=types.InputFile(excel_file, filename=filename))
        await message.answer("Полный отчет готов.", parse_mode=types.ParseMode.HTML)

def create_excel_report(records, sheet_name):
    wb = Workbook()
    ws = wb.active
    ws.title = sheet_name

    headers = ["ID", "Номер Самоката", "Сервис", "ID Пользователя", "Имя пользователя (ник)", "Полное имя пользователя", "Время Принятия"]
    ws.append(headers)

    header_font = Font(bold=True) 
    for cell in ws[1]:
        cell.font = header_font
        cell.alignment = Alignment(horizontal='center', vertical='center')

    for row_data in records:
        timestamp_str = row_data[6] 
        try:
            dt_stored = datetime.datetime.strptime(timestamp_str, "%Y-%m-%d %H:%M:%S")
            dt_aware = TIMEZONE.localize(dt_stored)
            row_data_list = list(row_data) 
            row_data_list[6] = dt_aware.strftime("%Y-%m-%d %H:%M:%S %Z%z")
            ws.append(row_data_list)
        except ValueError:
            ws.append(row_data)
        except Exception as e:
            print(f"Ошибка при обработке времени для Excel: {e} - Данные: {row_data}")
            ws.append(row_data)

    for col in ws.columns:
        max_length = 0
        column = col[0].column_letter
        for cell in col:
            try:
                if len(str(cell.value)) > max_length:
                    max_length = len(str(cell.value))
            except:
                pass
        adjusted_width = (max_length + 2)
        ws.column_dimensions[column].width = adjusted_width

    buffer = BytesIO()
    wb.save(buffer)
    buffer.seek(0)
    return buffer

# Handler for messages containing scooter numbers (for allowed users in allowed chats)
@dp.message_handler(content_types=[types.ContentType.TEXT, types.ContentType.PHOTO, types.ContentType.DOCUMENT, types.ContentType.VIDEO, types.ContentType.ANIMATION], 
                    is_allowed_chat=True, 
                    func=lambda message: not message.is_command() and (re.search(r'\b\d{6,8}\b', message.text if message.text else message.caption) is not None))
async def handle_scooter_numbers(message: types.Message) -> None:
    print(f"DEBUG: handle_scooter_numbers triggered for user {message.from_user.id} in chat {message.chat.id}. Text: '{message.text or message.caption}'")
    text_to_check = message.text if message.text else message.caption

    if not text_to_check or not text_to_check.strip(): 
        return 

    service_map = {
        "yandex": "Яндекс",
        "whoosh": "Whoosh",
        "jet": "Jet"
    }

    user_id = message.from_user.id
    username = message.from_user.username
    fullname = message.from_user.full_name

    total_accepted_from_user = 0
    accepted_by_service = {"Яндекс": 0, "Whoosh": 0, "Jet": 0}

    yandex_numbers = YANDEX_SCOOTER_PATTERN.findall(text_to_check)
    for num in yandex_numbers:
        insert_scooter_record(num, "Яндекс", user_id, username, fullname)
        accepted_by_service["Яндекс"] += 1
        total_accepted_from_user += 1

    woosh_numbers = WOOSH_SCOOTER_PATTERN.findall(text_to_check)
    for num in woosh_numbers:
        insert_scooter_record(num, "Whoosh", user_id, username, fullname)
        accepted_by_service["Whoosh"] += 1
        total_accepted_from_user += 1

    jet_numbers = JET_SCOOTER_PATTERN.findall(text_to_check)
    for num in jet_numbers:
        insert_scooter_record(num, "Jet", user_id, username, fullname)
        accepted_by_service["Jet"] += 1
        total_accepted_from_user += 1

    batch_text_matches = BATCH_TEXT_PATTERN.findall(text_to_check)
    timestamp_now_for_placeholder = datetime.datetime.now(TIMEZONE).strftime("%Y%m%d%H%M%S")

    for match in batch_text_matches:
        service_raw = match[0].lower()
        quantity_str = match[1]
        
        service = service_map.get(service_raw)
        
        try:
            quantity = int(quantity_str)
            if quantity > 0:
                for i in range(quantity):
                    placeholder_number = f"{service.upper()}_BATCH_{timestamp_now_for_placeholder}_{i+1}"
                    insert_scooter_record(placeholder_number, service, user_id, username, fullname)
                    accepted_by_service[service] += 1
                    total_accepted_from_user += 1
        except ValueError:
            pass 
            
    if total_accepted_from_user > 0:
        response_parts = []
        user_mention_text = message.from_user.full_name
        if message.from_user.username:
            user_mention = f"@{message.from_user.username}"
        else:
            user_mention = f"<a href='tg://user?id={message.from_user.id}'>{user_mention_text}</a>"

        main_message = f"{user_mention}, принято от тебя {total_accepted_from_user} шт.:"
        response_parts.append(main_message)

        for service_name in ["Яндекс", "Whoosh", "Jet"]:
            count = accepted_by_service[service_name]
            if count > 0:
                response_parts.append(f"{service_name}: {count}")
        
        final_response = "\n".join(response_parts)
        await message.reply(final_response, parse_mode=types.ParseMode.HTML)
    else:
        # If the message got here but no numbers were found by the patterns
        # This can happen if the text_contains filter was too broad
        print(f"DEBUG: handle_scooter_numbers found no valid numbers in '{text_to_check}'.")


@dp.message_handler(content_types=types.ContentType.ANY)
async def handle_unallowed_messages(message: types.Message) -> None:
    print(f"DEBUG: handle_unallowed_messages triggered for user {message.from_user.id} in chat {message.chat.id}. Type: {message.chat.type}")
    
    # Ignore messages from the bot itself
    if message.from_user.id == bot.id:
        print(f"DEBUG: Ignoring message from self: {message.text or message.caption}")
        return
    
    # Case 1: Private chat and sender is NOT an admin
    if message.chat.type == types.ChatType.PRIVATE and message.from_user.id not in ADMIN_IDS:
        print(f"DEBUG: Private chat, non-admin user {message.from_user.id}. Replying...")
        await message.answer("Извините, я принимаю номера самокатов только в разрешенных группах. Администраторы могут использовать меня в личке.")
    
    # Case 2: Group chat, chat ID is NOT in ALLOWED_CHAT_IDS, AND sender is NOT an admin
    elif message.chat.type in [types.ChatType.GROUP, types.ChatType.SUPERGROUP] and \
         message.chat.id not in ALLOWED_CHAT_IDS and \
         message.from_user.id not in ADMIN_IDS:
        print(f"DEBUG: Unauthorized group chat {message.chat.id}, non-admin user {message.from_user.id}. Silently ignoring.")
        # Silently ignore to avoid spamming unauthorized groups
        pass 
    
    # Case 3: Message from an admin (but it wasn't a command or a recognized scooter number message)
    elif message.from_user.id in ADMIN_IDS:
        print(f"DEBUG: Admin user {message.from_user.id} sent unhandled message in chat {message.chat.id}. Ignoring silently.")
        pass 
    
    # Case 4: Any other unhandled case (e.g., in allowed chat but not a number, or weird content type)
    else:
        print(f"DEBUG: Unhandled message from user {message.from_user.id} in chat {message.chat.id}. Content: '{message.text or message.caption}'")
        pass


# --- ЗАПУСК БОТА ---
async def main() -> None:
    init_db()
    print("Бот запускается...")
    await dp.start_polling()
    print("Бот остановлен.")

if __name__ == "__main__":
    asyncio.run(main())
