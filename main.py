import asyncio
import re
import os
import sqlite3
import datetime
from io import BytesIO
import pytz

from aiogram import Bot, Dispatcher, types
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
    print("Внимание: ADMIN_IDS не заданы в .env файле. Функции администратора будут недоступны.")

ALLOWED_CHAT_IDS = [int(chat_id) for chat_id in os.getenv('ALLOWED_CHAT_IDS', '').split(',') if chat_id.strip()]
if not ALLOWED_CHAT_IDS:
    print("Внимание: ALLOWED_CHAT_IDS не задан. Бот будет работать только в личных сообщениях с администраторами.")


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
        return message.from_user.id in ADMIN_IDS

class IsAllowedChatFilter(BoundFilter):
    key = 'is_allowed_chat'
    def __init__(self, is_allowed_chat):
        self.is_allowed_chat = is_allowed_chat
    async def check(self, message: types.Message):
        # Разрешаем, если это личный чат с админом
        if message.chat.type == types.ChatType.PRIVATE and message.from_user.id in ADMIN_IDS:
            return True
        # Разрешаем, если это групповой чат из списка разрешенных
        if message.chat.type in [types.ChatType.GROUP, types.ChatType.SUPERGROUP] and message.chat.id in ALLOWED_CHAT_IDS:
            return True
        return False

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

### ИЗМЕНЕНИЕ ###
# Команда /start теперь тоже доступна только в разрешенных чатах (группа + личка админов)
@dp.message_handler(commands=['start'], is_allowed_chat=True)
async def command_start_handler(message: types.Message) -> None:
    allowed_chats_info = ', '.join(map(str, ALLOWED_CHAT_IDS)) if ALLOWED_CHAT_IDS else "не указаны"
    response = (f"Привет, {message.from_user.full_name}! Я бот для приёма самокатов.\n\n"
                f"Я работаю в группе с ID: `{allowed_chats_info}` и в личных сообщениях с администраторами.\n"
                f"Твой ID чата: `{message.chat.id}`")
    await message.answer(response, parse_mode=types.ParseMode.MARKDOWN)

# Пакетная сдача (логика не изменилась, фильтры IsAdmin и IsAllowedChat работают как нужно)
@dp.message_handler(commands=['batch_accept'], is_admin=True)
@dp.message_handler(commands=['batch_accept'], is_allowed_chat=True)
async def batch_accept_handler(message: types.Message) -> None:
    args = message.get_args().split()
    if len(args) != 2:
        await message.reply("Используйте: `/batch_accept <сервис> <количество>`\nПример: `/batch_accept Yandex 20`")
        return

    service_raw, quantity_str = args
    service_map = {"yandex": "Яндекс", "whoosh": "Whoosh", "jet": "Jet"}
    service = service_map.get(service_raw.lower())

    if not service:
        await message.reply("Неизвестный сервис. Доступны: `Yandex`, `Whoosh`, `Jet`.")
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
    timestamp_now_for_placeholder = datetime.datetime.now(TIMEZONE).strftime("%Y%m%d%H%M%S") 

    for i in range(quantity):
        placeholder_number = f"{service.upper()}_BATCH_{timestamp_now_for_placeholder}_{i+1}"
        insert_scooter_record(placeholder_number, service, user_id, username, fullname)
    
    user_mention = f"@{username}" if username else f"<a href='tg://user?id={user_id}'>{fullname}</a>"
    await message.reply(f"{user_mention}, принято {quantity} самокатов сервиса {service}.", parse_mode=types.ParseMode.HTML)

# Админские команды (только для админов, без изменений)
@dp.message_handler(commands=['today_stats', 'export_today_excel', 'export_all_excel'], is_admin=True)
async def admin_commands_handler(message: types.Message) -> None:
    command = message.get_command(pure=True)
    date_filter = None
    if command == 'today_stats':
        date_filter = 'today'
    elif command in ['export_today_excel', 'export_all_excel']:
        date_filter = 'today' if 'today' in command else 'all'
    
    if command == 'today_stats':
        records = get_scooter_records(date_filter='today')
        # ... (логика статистики осталась прежней)
        if not records:
            await message.answer("Сегодня пока ничего не принято.")
            return

        users_stats = {}
        for _, _, service, user_id, username, fullname, _ in records:
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
    
    else: # export commands
        await message.answer(f"Формирую отчет{' за сегодня' if date_filter == 'today' else ' за все время'}...")
        records = get_scooter_records(date_filter=date_filter)
        if not records:
            await message.answer("Нет данных для экспорта.")
            return
        
        report_type = "today" if date_filter == "today" else "full"
        excel_file = create_excel_report(records, f"Отчет {report_type}")
        filename = f"report_{report_type}_{datetime.date.today().isoformat()}.xlsx"
        await bot.send_document(message.chat.id, types.InputFile(excel_file, filename=filename), caption="Ваш отчет готов.")


def create_excel_report(records, sheet_name):
    # ... (функция создания Excel осталась прежней)
    wb = Workbook()
    ws = wb.active
    ws.title = sheet_name
    headers = ["ID", "Номер Самоката", "Сервис", "ID Пользователя", "Ник", "Полное имя", "Время Принятия"]
    ws.append(headers)
    header_font = Font(bold=True)
    for cell in ws[1]: cell.font = header_font
    
    for row in records: ws.append(row)

    for col in ws.columns:
        max_length = max(len(str(cell.value)) for cell in col if cell.value)
        ws.column_dimensions[col[0].column_letter].width = max_length + 2

    buffer = BytesIO()
    wb.save(buffer)
    buffer.seek(0)
    return buffer

# Обработчик номеров (срабатывает только в разрешенных чатах)
@dp.message_handler(
    lambda message: not message.is_command(),
    content_types=types.ContentType.ANY, # Принимаем любой контент, чтобы извлечь подпись
    is_allowed_chat=True
)
async def handle_scooter_numbers(message: types.Message) -> None:
    text_to_check = message.text or message.caption
    if not text_to_check: return

    user_id = message.from_user.id
    username = message.from_user.username
    fullname = message.from_user.full_name

    accepted_by_service = {"Яндекс": 0, "Whoosh": 0, "Jet": 0}
    
    # ... (логика поиска номеров осталась прежней)
    patterns = {
        "Яндекс": YANDEX_SCOOTER_PATTERN,
        "Whoosh": WOOSH_SCOOTER_PATTERN,
        "Jet": JET_SCOOTER_PATTERN
    }
    for service, pattern in patterns.items():
        numbers = pattern.findall(text_to_check)
        for num in numbers:
            insert_scooter_record(num, service, user_id, username, fullname)
            accepted_by_service[service] += 1
    
    total_accepted = sum(accepted_by_service.values())
    if total_accepted > 0:
        response_parts = []
        user_mention = f"@{username}" if username else f"<a href='tg://user?id={user_id}'>{fullname}</a>"
        response_parts.append(f"{user_mention}, принято от тебя {total_accepted} шт.:")
        for service, count in accepted_by_service.items():
            if count > 0: response_parts.append(f"{service}: {count}")
        await message.reply("\n".join(response_parts), parse_mode=types.ParseMode.HTML)


### ИЗМЕНЕНИЕ ###
# Этот обработчик ловит ВСЕ ОСТАЛЬНЫЕ сообщения, которые не подошли под правила выше.
# Он должен стоять последним.
# Его задача - молчать и ничего не делать, чтобы бот не реагировал в "чужих" чатах.
@dp.message_handler(content_types=types.ContentType.ANY)
async def handle_unallowed_messages(message: types.Message) -> None:
    # Просто игнорируем все сообщения, которые дошли до этого обработчика.
    # Это значит, что сообщение было отправлено:
    # - в личный чат не от администратора
    # - в группу, которой нет в списке ALLOWED_CHAT_IDS
    # Согласно требованиям, бот должен в этих случаях молчать.
    return

# --- ЗАПУСК БОТА ---
async def on_startup(dp):
    init_db()
    print("База данных инициализирована.")
    print("Бот запущен.")

async def on_shutdown(dp):
    print("Бот остановлен.")

if __name__ == "__main__":
    executor.start_polling(dp, skip_updates=True, on_startup=on_startup, on_shutdown=on_shutdown)
