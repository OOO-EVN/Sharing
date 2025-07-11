import asyncio
import re
import os
import sqlite3
import datetime
from io import BytesIO

from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import CommandStart, Command
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

# --- ИНИЦИАЛИЗАЦИЯ ---
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(parse_mode=ParseMode.HTML) # Важно для упоминаний по ID

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

# --- КОМАНДЫ АДМИНИСТРАТОРА ---

@dp.message(lambda message: is_admin(message.from_user.id), Command("today_stats"))
async def admin_today_stats_handler(message: types.Message) -> None:
    records = get_scooter_records(date_filter='today')
    
    if not records:
        await message.answer("Сегодня пока ничего не принято.")
        return

    stats = {}
    for record in records:
        service = record[2]
        stats[service] = stats.get(service, 0) + 1
    
    response = "Статистика за сегодня:\n"
    for service, count in stats.items():
        response += f"Принято {service}: {count}\n"
    
    response += f"\nВсего сегодня принято: {len(records)}"
    await message.answer(response)

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
    await message.answer("Полный отчет готов.")


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

    if text_to_check:
        response_parts = []
        
        user_id = message.from_user.id
        username = message.from_user.username
        fullname = message.from_user.full_name

        total_accepted_from_user = 0 # Новый счетчик для общего количества от этого пользователя

        yandex_numbers = YANDEX_SCOOTER_PATTERN.findall(text_to_check)
        yandex_count = len(yandex_numbers)
        if yandex_count > 0:
            response_parts.append(f"Яндекс: {yandex_count}")
            total_accepted_from_user += yandex_count
            for num in yandex_numbers:
                insert_scooter_record(num, "Яндекс", user_id, username, fullname)

        woosh_numbers = WOOSH_SCOOTER_PATTERN.findall(text_to_check)
        woosh_count = len(woosh_numbers)
        if woosh_count > 0:
            response_parts.append(f"Whoosh: {woosh_count}")
            total_accepted_from_user += woosh_count
            for num in woosh_numbers:
                insert_scooter_record(num, "Whoosh", user_id, username, fullname)

        jet_numbers = JET_SCOOTER_PATTERN.findall(text_to_check)
        jet_count = len(jet_numbers)
        if jet_count > 0:
            response_parts.append(f"Jet: {jet_count}")
            total_accepted_from_user += jet_count
            for num in jet_numbers:
                insert_scooter_record(num, "Jet", user_id, username, fullname)

        if response_parts:
            # Получаем информацию об отправителе для "грубого" упоминания
            user_mention_text = message.from_user.full_name
            if message.from_user.username:
                user_mention = f"@{message.from_user.username}"
            else:
                user_mention = f"<a href='tg://user?id={message.from_user.id}'>{user_mention_text}</a>"

            # Формируем "грубый" ответ
            if total_accepted_from_user > 0:
                # Используем bold для выделения
                main_message = f"<b>{user_mention}, принято от тебя {total_accepted_from_user} шт.:</b>"
            else: # На случай, если каким-то образом response_parts не пуст, но счетчик 0
                main_message = f"<b>{user_mention}, принято от тебя:</b>"
            
            # Объединяем основное сообщение с деталями по сервисам
            final_response = main_message + "\n" + "\n".join(response_parts)
            
            await message.reply(final_response)

# --- ЗАПУСК БОТА ---
async def main() -> None:
    init_db()
    print("Бот запускается...")
    await dp.start_polling(bot)
    print("Бот остановлен.")

if __name__ == "__main__":
    asyncio.run(main()
