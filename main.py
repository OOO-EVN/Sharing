import asyncio
import re
import os
import sqlite3
import datetime
from io import BytesIO

from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import CommandStart, Command, CommandObject 
from aiogram.enums import ParseMode
from dotenv import load_dotenv
from openpyxl import Workbook
from openpyxl.styles import Font, Alignment

# Загружаем переменные окружения из .env файла
load_dotenv()

# --- КОНСТАНТЫ ---
BOT_TOKEN = os.getenv('BOT_TOKEN')
if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN не найден в .env файле. Пожалуйста, добавьте его.")

ADMIN_IDS = [int(admin_id) for admin_id in os.getenv('ADMIN_IDS', '').split(',') if admin_id.strip()]
if not ADMIN_IDS:
    print("Внимание: ADMIN_IDS не заданы в .env файле. Функции администратора будут недоступны.")

DB_NAME = 'scooters.db'

# Регулярные выражения для определения сервиса:
YANDEX_SCOOTER_PATTERN = re.compile(r'\b\d{8}\b')
WOOSH_SCOOTER_PATTERN = re.compile(r'\b[A-Z]{2}\d{4}\b', re.IGNORECASE)
JET_SCOOTER_PATTERN = re.compile(r'\b\d{6}\b')

# НОВОЕ: Регулярное выражение для распознавания пакетных записей в свободном тексте
BATCH_TEXT_PATTERN = re.compile(r'(yandex|whoosh|jet)\s+(\d+)', re.IGNORECASE)

# --- ИНИЦИАЛИЗАЦИЯ ---
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(parse_mode=ParseMode.HTML) 

# --- ФУНКЦИИ БАЗЫ ДАННЫХ ---
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
    cursor.execute('''
        INSERT INTO accepted_scooters (scooter_number, service, accepted_by_user_id, accepted_by_username, accepted_by_fullname)
        VALUES (?, ?, ?, ?, ?)
    ''', (scooter_number, service, user_id, username, fullname))
    conn.commit()
    conn.close()

def get_scooter_records(date_filter=None):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    if date_filter == 'today':
        cursor.execute("SELECT * FROM accepted_scooters WHERE DATE(timestamp, 'localtime') = DATE('now', 'localtime')")
    else:
        cursor.execute("SELECT * FROM accepted_scooters")
    records = cursor.fetchall()
    conn.close()
    return records

# --- ФУНКЦИЯ ПРОВЕРКИ АДМИНА ---
def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS

# --- ОБРАБОТЧИКИ СООБЩЕНИЙ ---

@dp.message(CommandStart())
async def command_start_handler(message: types.Message) -> None:
    await message.answer(f"Привет, {message.from_user.full_name}! Я готов принимать самокаты.")

# --- КОМАНДА ДЛЯ ПАКЕТНОЙ СДАЧИ (остается как опция) ---
@dp.message(Command("batch_accept"))
async def batch_accept_handler(message: types.Message, command: CommandObject) -> None:
    """
    Принимает пакетную сдачу самокатов: /batch_accept <сервис> <количество>
    """
    args = command.args.split()
    if len(args) != 2:
        await message.reply("Используйте команду в формате: /batch_accept <сервис> <количество>\nНапример: /batch_accept Yandex 20")
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
        await message.reply("Неизвестный сервис. Доступные сервисы: Yandex, Whoosh, Jet.")
        return

    try:
        quantity = int(quantity_str)
        if quantity <= 0:
            raise ValueError("Количество должно быть положительным числом.")
    except ValueError:
        await message.reply("Количество должно быть положительным числом.")
        return

    user_id = message.from_user.id
    username = message.from_user.username
    fullname = message.from_user.full_name

    accepted_count = 0
    timestamp_now = datetime.datetime.now().strftime("%Y%m%d%H%M%S") 

    for i in range(quantity):
        placeholder_number = f"{service.upper()}_BATCH_{timestamp_now}_{i+1}"
        insert_scooter_record(placeholder_number, service, user_id, username, fullname)
        accepted_count += 1

    user_mention_text = message.from_user.full_name
    if message.from_user.username:
        user_mention = f"@{message.from_user.username}"
    else:
        user_mention = f"<a href='tg://user?id={message.from_user.id}'>{user_mention_text}</a>"

    await message.reply(
        f"{user_mention}, принято {accepted_count} самокатов сервиса {service} в качестве пакетной сдачи."
    )


# --- КОМАНДЫ АДМИНИСТРАТОРА (без изменений) ---

@dp.message(lambda message: is_admin(message.from_user.id), Command("today_stats"))
async def admin_today_stats_handler(message: types.Message) -> None:
    records = get_scooter_records(date_filter='today')
    
    if not records:
        await message.answer("Сегодня пока ничего не принято.")
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
    
    await message.answer(final_response)


@dp.message(lambda message: is_admin(message.from_user.id), Command("export_today_excel"))
async def admin_export_today_excel_handler(message: types.Message) -> None:
    await message.answer("Формирую отчет за сегодня, пожалуйста, подождите...")
    records = get_scooter_records(date_filter='today')
    if not records:
        await message.answer("Нет данных за сегодня для экспорта.")
        return

    excel_file = create_excel_report(records, "Отчет за сегодня")
    filename = f"report_today_{datetime.date.today().isoformat()}.xlsx"
    await message.answer_document(types.FSInputFile(excel_file, filename=filename))
    await message.answer("Отчет за сегодня готов.")

@dp.message(lambda message: is_admin(message.from_user.id), Command("export_all_excel"))
async def admin_export_all_excel_handler(message: types.Message) -> None:
    await message.answer("Формирую полный отчет, пожалуйста, подождите...")
    records = get_scooter_records(date_filter='all')
    if not records:
        await message.answer("Нет данных для экспорта.")
        return

    excel_file = create_excel_report(records, "Полный отчет")
    filename = f"full_report_{datetime.date.today().isoformat()}.xlsx"
    await message.answer_document(types.FSInputFile(excel_file, filename=filename))
    await message.answer("Отчет готов.")


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


@dp.message()
async def handle_all_messages(message: types.Message) -> None:
    text_to_check = ""

    if message.text:
        text_to_check += message.text
    if message.caption:
        text_to_check += " " + message.caption

    if not text_to_check.strip(): # Проверяем, что текст не пустой или состоит только из пробелов
        return # Если сообщение пустое, ничего не делаем

    # Словарь для приведения сервиса к нужному формату для БД
    service_map = {
        "yandex": "Яндекс",
        "whoosh": "Whoosh",
        "jet": "Jet"
    }

    user_id = message.from_user.id
    username = message.from_user.username
    fullname = message.from_user.full_name

    total_accepted_from_user = 0
    # Отслеживаем количество по каждому сервису для ответа
    accepted_by_service = {"Яндекс": 0, "Whoosh": 0, "Jet": 0}

    # --- 1. Обработка ИНДИВИДУАЛЬНЫХ номеров самокатов (как и раньше) ---
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

    # --- 2. Обработка ПАКЕТНЫХ записей из текста (НОВОЕ) ---
    batch_text_matches = BATCH_TEXT_PATTERN.findall(text_to_check)
    timestamp_now = datetime.datetime.now().strftime("%Y%m%d%H%M%S") # Единая метка для пакета

    for match in batch_text_matches:
        service_raw = match[0].lower()
        quantity_str = match[1]
        
        service = service_map.get(service_raw)
        
        try:
            quantity = int(quantity_str)
            if quantity > 0:
                for i in range(quantity):
                    # Генерируем уникальный номер-заглушку для каждой записи в пакете
                    placeholder_number = f"{service.upper()}_BATCH_{timestamp_now}_{i+1}"
                    insert_scooter_record(placeholder_number, service, user_id, username, fullname)
                    accepted_by_service[service] += 1
                    total_accepted_from_user += 1
        except ValueError:
            # Игнорируем неверные количества, не ломаем обработку других записей
            pass 
            
    # --- Формирование ответа ---
    if total_accepted_from_user > 0:
        response_parts = []
        user_mention_text = message.from_user.full_name
        if message.from_user.username:
            user_mention = f"@{message.from_user.username}"
        else:
            user_mention = f"<a href='tg://user?id={message.from_user.id}'>{user_mention_text}</a>"

        main_message = f"{user_mention}, принято от тебя {total_accepted_from_user} шт.:"
        response_parts.append(main_message)

        # Добавляем детализацию по сервисам, только если есть принятые самокаты по ним
        for service_name in ["Яндекс", "Whoosh", "Jet"]:
            count = accepted_by_service[service_name]
            if count > 0:
                response_parts.append(f"{service_name}: {count}")
        
        final_response = "\n".join(response_parts)
        await message.reply(final_response)

# --- ЗАПУСК БОТА ---
async def main() -> None:
    init_db()
    print("Бот запускается...")
    await dp.start_polling(bot)
    print("Бот остановлен.")

if __name__ == "__main__":
    asyncio.run(main())
