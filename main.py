import asyncio
import re
import os
import sqlite3
import datetime
from io import BytesIO
import pytz # Импорт для работы с часовыми поясами

from aiogram import Bot, Dispatcher, types
from aiogram.dispatcher.filters import Command
from aiogram.dispatcher.filters import BoundFilter
from aiogram.utils import executor # Для запуска бота

from dotenv import load_dotenv
from openpyxl import Workbook
from openpyxl.styles import Font, Alignment

# Загружаем переменные окружения из файла .env
load_dotenv()

# --- КОНСТАНТЫ ---
BOT_TOKEN = os.getenv('BOT_TOKEN')
if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN не найден в .env файле. Пожалуйста, добавьте его.")

# Парсим ID администраторов из переменной окружения
ADMIN_IDS = [int(admin_id) for admin_id in os.getenv('ADMIN_IDS', '').split(',') if admin_id.strip()]
if not ADMIN_IDS:
    print("Внимание: ADMIN_IDS не заданы в .env файле. Пожалуйста, добавьте ID администраторов.")

# Парсим ID разрешенных чатов/групп из переменной окружения
ALLOWED_CHAT_IDS = [int(chat_id) for chat_id in os.getenv('ALLOWED_CHAT_IDS', '').split(',') if chat_id.strip()]
if not ALLOWED_CHAT_IDS:
    print("Внимание: ALLOWED_CHAT_IDS не заданы в .env файле. Бот будет принимать номера только в личке с админами.")

DB_NAME = 'scooters.db' # Имя файла базы данных SQLite

# Часовой пояс для Алматы (Казахстан). UTC+5
TIMEZONE = pytz.timezone('Asia/Almaty') 

# Регулярные выражения для определения сервиса самоката:
YANDEX_SCOOTER_PATTERN = re.compile(r'\b\d{8}\b') # 8 цифр
WOOSH_SCOOTER_PATTERN = re.compile(r'\b[A-Z]{2}\d{4}\b', re.IGNORECASE) # 2 буквы + 4 цифры
JET_SCOOTER_PATTERN = re.compile(r'\b\d{6}\b') # 6 цифр

# Регулярное выражение для распознавания пакетных записей в свободном тексте
BATCH_TEXT_PATTERN = re.compile(r'(yandex|whoosh|jet)\s+(\d+)', re.IGNORECASE)

# --- ИНИЦИАЛИЗАЦИЯ БОТА И ДИСПЕТЧЕРА ---
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(bot) 

# --- КЛАССЫ ФИЛЬТРОВ ---

# Фильтр для проверки, является ли пользователь администратором
class IsAdminFilter(BoundFilter):
    key = 'is_admin'

    def __init__(self, is_admin):
        self.is_admin = is_admin

    async def check(self, message: types.Message):
        return message.from_user.id in ADMIN_IDS

# Фильтр для проверки, находится ли сообщение в разрешенном чате (группе)
class IsAllowedChatFilter(BoundFilter):
    key = 'is_allowed_chat'

    def __init__(self, is_allowed_chat):
        self.is_allowed_chat = is_allowed_chat

    async def check(self, message: types.Message):
        if not self.is_allowed_chat:
            return False
        # Проверяем, является ли чат разрешенным для приема номеров
        return message.chat.id in ALLOWED_CHAT_IDS

# Регистрация созданных фильтров в диспетчере
dp.filters_factory.bind(IsAdminFilter)
dp.filters_factory.bind(IsAllowedChatFilter)


# --- ФУНКЦИИ ВЗАИМОДЕЙСТВИЯ С БАЗОЙ ДАННЫХ ---

# Инициализация базы данных: создание таблицы, если она не существует
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

# Вставка записи о самокате в базу данных
def insert_scooter_record(scooter_number, service, user_id, username, fullname):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    
    # Получаем текущее время в указанном часовом поясе (UTC+5)
    now_localized = datetime.datetime.now(TIMEZONE)
    timestamp_str = now_localized.strftime("%Y-%m-%d %H:%M:%S") # Формат для SQLite

    cursor.execute('''
        INSERT INTO accepted_scooters (scooter_number, service, accepted_by_user_id, accepted_by_username, accepted_by_fullname, timestamp)
        VALUES (?, ?, ?, ?, ?, ?)
    ''', (scooter_number, service, user_id, username, fullname, timestamp_str))
    conn.commit()
    conn.close()

# Получение записей о самокатах из базы данных (с фильтром по дате или все)
def get_scooter_records(date_filter=None):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    if date_filter == 'today':
        # При фильтрации по "сегодня" используем локализованную дату
        today_localized = datetime.datetime.now(TIMEZONE).strftime("%Y-%m-%d")
        cursor.execute("SELECT * FROM accepted_scooters WHERE DATE(timestamp) = ?", (today_localized,))
    else:
        cursor.execute("SELECT * FROM accepted_scooters")
    records = cursor.fetchall()
    conn.close()
    return records

# --- ОБРАБОТЧИКИ СООБЩЕНИЙ ---

# Обработчик команды /start
@dp.message_handler(commands=['start'])
async def command_start_handler(message: types.Message) -> None:
    # Собираем список разрешенных чатов для отображения пользователю
    allowed_chats_info = ', '.join(map(str, ALLOWED_CHAT_IDS)) if ALLOWED_CHAT_IDS else "не указаны (бот принимает номера только от админов)"
    
    response = (f"Привет, {message.from_user.full_name}! Я бот для приёма самокатов.\n\n"
                f"Я принимаю номера самокатов в группах с ID: `{allowed_chats_info}`.\n"
                f"Если ты админ, можешь сдавать самокаты и получать отчёты в любом чате.\n\n"
                f"Просто отправь номер самоката или фото с номером в подписи. "
                f"Для пакетной сдачи используй `/batch_accept <сервис> <количество>`.\n\n"
                f"Твой ID чата: `{message.chat.id}`" # Полезно для получения ID группы при настройке
               )
    await message.answer(response, parse_mode=types.ParseMode.MARKDOWN)

# Обработчик команды /batch_accept
# Этот обработчик срабатывает для всех, кто в разрешенном чате (включая админов)
# и для админов в любом чате (благодаря order='HIGH')
@dp.message_handler(commands=['batch_accept'], is_allowed_chat=True)
@dp.message_handler(commands=['batch_accept'], is_admin=True, state=None, run_task=True) # Админы могут использовать везде
async def batch_accept_handler(message: types.Message) -> None:
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
    # Используем локализованное время для генерации placeholder_number
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

# Обработчик админской команды /today_stats
@dp.message_handler(commands=['today_stats'], is_admin=True)
async def admin_today_stats_handler(message: types.Message) -> None:
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
        response_parts.append("Деп\n") # Разделитель для читаемости между пользователями
        total_all_users += user_total

    # Удаляем последний разделитель, если он лишний
    if response_parts and response_parts[-1] == "Деп\n":
        response_parts[-1] = "Деп"

    final_response = "\n".join(response_parts)
    
    final_response += f"\n---\nОбщий итог за сегодня: {total_all_users} шт."
    
    await message.answer(final_response, parse_mode=types.ParseMode.HTML)

# Обработчик админской команды /export_today_excel
@dp.message_handler(commands=['export_today_excel'], is_admin=True)
async def admin_export_today_excel_handler(message: types.Message) -> None:
    await message.answer("Формирую отчет за сегодня, пожалуйста, подождите...", parse_mode=types.ParseMode.HTML)
    records = get_scooter_records(date_filter='today')
    if not records:
        await message.answer("Нет данных за сегодня для экспорта.", parse_mode=types.ParseMode.HTML)
        return

    excel_file = create_excel_report(records, "Отчет за сегодня")
    filename = f"report_today_{datetime.date.today().isoformat()}.xlsx"
    await bot.send_document(chat_id=message.chat.id, document=types.InputFile(excel_file, filename=filename))
    await message.answer("Отчет за сегодня готов.", parse_mode=types.ParseMode.HTML)

# Обработчик админской команды /export_all_excel
@dp.message_handler(commands=['export_all_excel'], is_admin=True)
async def admin_export_all_excel_handler(message: types.Message) -> None:
    await message.answer("Формирую полный отчет, пожалуйста, подождите...", parse_mode=types.ParseMode.HTML)
    records = get_scooter_records(date_filter='all')
    if not records:
        await message.answer("Нет данных для экспорта.", parse_mode=types.ParseMode.HTML)
        return

    excel_file = create_excel_report(records, "Полный отчет")
    filename = f"full_report_{datetime.date.today().isoformat()}.xlsx"
    await bot.send_document(chat_id=message.chat.id, document=types.InputFile(excel_file, filename=filename))
    await message.answer("Полный отчет готов.", parse_mode=types.ParseMode.HTML)

# Функция для создания Excel-отчета
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
        timestamp_str = row_data[6] # Время находится в последней колонке
        try:
            # Парсим время из строки (оно уже должно быть в TIMEZONE)
            dt_stored = datetime.datetime.strptime(timestamp_str, "%Y-%m-%d %H:%M:%S")
            # Делаем его aware (осведомленным) о TIMEZONE, чтобы форматировать с зоной
            dt_aware = TIMEZONE.localize(dt_stored)
            # Форматируем для Excel, добавляя информацию о часовом поясе
            row_data_list = list(row_data) # Конвертируем в список для изменения
            row_data_list[6] = dt_aware.strftime("%Y-%m-%d %H:%M:%S %Z%z") # Пример: "2025-07-12 01:00:00 ALMT+0500"
            ws.append(row_data_list)
        except ValueError:
            # Если формат времени не соответствует или есть другие ошибки, добавляем как есть
            ws.append(row_data)
        except Exception as e:
            print(f"Ошибка при обработке времени для Excel: {e} - Данные: {row_data}")
            ws.append(row_data) # Добавить исходные данные, если произошла ошибка

    # Автоматическая настройка ширины столбцов
    for col in ws.columns:
        max_length = 0
        column = col[0].column_letter # Получаем букву столбца
        for cell in col:
            try:
                if len(str(cell.value)) > max_length:
                    max_length = len(str(cell.value))
            except:
                pass
        adjusted_width = (max_length + 2) # Добавляем небольшой запас
        ws.column_dimensions[column].width = adjusted_width

    buffer = BytesIO()
    wb.save(buffer)
    buffer.seek(0)
    return buffer

# Обработчик для сообщений, содержащих номера самокатов (для обычных пользователей в разрешенных чатах)
# Также срабатывает для админов, так как админы тоже могут кидать номера.
@dp.message_handler(content_types=[types.ContentType.TEXT, types.ContentType.PHOTO, types.ContentType.DOCUMENT, types.ContentType.VIDEO, types.ContentType.ANIMATION], is_allowed_chat=True)
async def handle_allowed_chat_messages(message: types.Message) -> None:
    text_to_check = message.text if message.text else message.caption

    # Игнорируем пустые сообщения или медиа без подписи
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

    # Поиск номеров Яндекс
    yandex_numbers = YANDEX_SCOOTER_PATTERN.findall(text_to_check)
    for num in yandex_numbers:
        insert_scooter_record(num, "Яндекс", user_id, username, fullname)
        accepted_by_service["Яндекс"] += 1
        total_accepted_from_user += 1

    # Поиск номеров Whoosh
    woosh_numbers = WOOSH_SCOOTER_PATTERN.findall(text_to_check)
    for num in woosh_numbers:
        insert_scooter_record(num, "Whoosh", user_id, username, fullname)
        accepted_by_service["Whoosh"] += 1
        total_accepted_from_user += 1

    # Поиск номеров Jet
    jet_numbers = JET_SCOOTER_PATTERN.findall(text_to_check)
    for num in jet_numbers:
        insert_scooter_record(num, "Jet", user_id, username, fullname)
        accepted_by_service["Jet"] += 1
        total_accepted_from_user += 1

    # Поиск пакетных записей (например, "yandex 10")
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
            pass # Игнорируем некорректные пакетные записи
            
    # Отправляем ответ пользователю, если что-то было принято
    if total_accepted_from_user > 0:
        response_parts = []
        user_mention_text = message.from_user.full_name
        # Формируем упоминание пользователя
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

# Универсальный обработчик для всех сообщений, которые не были обработаны предыдущими.
# Он должен быть ПОСЛЕДНИМ в файле, чтобы не перехватывать другие хендлеры.
@dp.message_handler(content_types=types.ContentType.ANY)
async def handle_unallowed_messages(message: types.Message) -> None:
    # Если это приватный чат и отправитель НЕ админ
    if message.chat.type == types.ChatType.PRIVATE and message.from_user.id not in ADMIN_IDS:
        await message.answer("Извините, я принимаю номера самокатов только в разрешенных группах. Администраторы могут использовать меня в личке.")
    # Если это групповой чат, но его ID нет в списке ALLOWED_CHAT_IDS
    elif message.chat.type in [types.ChatType.GROUP, types.ChatType.SUPERGROUP] and message.chat.id not in ALLOWED_CHAT_IDS:
        # Для неразрешенных групп лучше ничего не отвечать, чтобы не показывать активность и не спамить.
        pass 
    # Если это админ, и его сообщение не было обработано (например, некорректная команда, или просто случайный текст)
    elif message.from_user.id in ADMIN_IDS:
        pass # Админы могут получать неответы на случайные сообщения
    else:
        # Все остальные случаи (редкие, например, неизвестный тип контента)
        pass

# --- ЗАПУСК БОТА ---
async def main() -> None:
    init_db() # Инициализируем базу данных при запуске
    print("Бот запускается...")
    # Начинаем опрос Telegram API
    await dp.start_polling()
    print("Бот остановлен.")

if __name__ == "__main__":
    asyncio.run(main()) # Запускаем асинхронную функцию main
