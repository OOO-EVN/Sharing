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
from aiogram.dispatcher.filters import BoundFilter
from aiogram.utils.exceptions import MessageIsTooLong

from dotenv import load_dotenv
from openpyxl import Workbook
from openpyxl.styles import Font, Alignment
from openpyxl.utils import get_column_letter

# Для планировщика задач (для автоматических отчетов)
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# Настройка логирования
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# Загрузка переменных окружения из .env файла
load_dotenv()

# Получение токена бота из переменных окружения
BOT_TOKEN = os.getenv('BOT_TOKEN')
if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN не найден в .env файле. Пожалуйста, добавьте его.")

# Получение ID администраторов, разрешенных чатов и чатов для отчетов
try:
    ADMIN_IDS = {int(admin_id) for admin_id in os.getenv('ADMIN_IDS', '').split(',') if admin_id.strip()}
    ALLOWED_CHAT_IDS = {int(chat_id) for chat_id in os.getenv('ALLOWED_CHAT_IDS', '').split(',') if chat_id.strip()}
    REPORT_CHAT_IDS = {int(chat_id) for chat_id in os.getenv('REPORT_CHAT_IDS', '').split(',') if chat_id.strip()}
except ValueError:
    logging.error("Не удалось прочитать ADMIN_IDS, ALLOWED_CHAT_IDS или REPORT_CHAT_IDS. Убедитесь, что они являются числами, разделенными запятыми.")
    ADMIN_IDS = set()
    ALLOWED_CHAT_IDS = set()
    REPORT_CHAT_IDS = set()

# Настройки базы данных и временной зоны
DB_NAME = 'scooters.db'
TIMEZONE = pytz.timezone('Asia/Almaty') # Ваша таймзона UTC+5

# Регулярные выражения для номеров самокатов различных сервисов
YANDEX_SCOOTER_PATTERN = re.compile(r'\b(\d{8})\b')
WOOSH_SCOOTER_PATTERN = re.compile(r'\b([A-ZА-Я]{2}\d{4})\b', re.IGNORECASE)
JET_SCOOTER_PATTERN = re.compile(r'\b(\d{3}-?\d{3})\b')

# Регулярное выражение и алиасы для пакетного приема
BATCH_QUANTITY_PATTERN = re.compile(r'\b(whoosh|jet|yandex|вуш|джет|яндекс|w|j|y)\s+(\d+)\b', re.IGNORECASE)
SERVICE_ALIASES = {
    "yandex": "Яндекс", "яндекс": "Яндекс", "y": "Яндекс",
    "whoosh": "Whoosh", "вуш": "Whoosh", "w": "Whoosh",
    "jet": "Jet", "джет": "Jet", "j": "Jet"
}
SERVICE_MAP = {"yandex": "Яндекс", "whoosh": "Whoosh", "jet": "Jet"} # На случай если где-то используется прямое сопоставление

# Инициализация бота и диспетчера
bot = Bot(token=BOT_TOKEN, parse_mode="HTML")
storage = MemoryStorage()
dp = Dispatcher(bot, storage=storage)

# Глобальные переменные для пула потоков БД и планировщика
db_executor = None
scheduler = None

# --- Фильтры ---
class IsAdminFilter(BoundFilter):
    """Фильтр для проверки, является ли отправитель сообщения администратором."""
    async def check(self, message: types.Message) -> bool:
        return message.from_user.id in ADMIN_IDS

class IsAllowedChatFilter(BoundFilter):
    """Фильтр для проверки, разрешен ли данный чат для работы бота."""
    async def check(self, message: types.Message) -> bool:
        # Разрешаем администраторам использовать бота в личных сообщениях
        if message.chat.type == 'private' and message.from_user.id in ADMIN_IDS:
            return True
        # Разрешаем в указанных группах
        if message.chat.type in ['group', 'supergroup'] and message.chat.id in ALLOWED_CHAT_IDS:
            return True
        logging.warning(f"Сообщение от {message.from_user.id} в чате {message.chat.id} было заблокировано фильтром IsAllowedChatFilter.")
        return False

# --- Функции для работы с базой данных ---
def run_db_query(query: str, params: tuple = (), fetch: str = None):
    """
    Выполняет SQL-запрос к базе данных.
    :param query: SQL-запрос.
    :param params: Параметры запроса.
    :param fetch: 'one' для получения одной записи, 'all' для всех записей, None для без возврата (INSERT/UPDATE/DELETE).
    :return: Результат запроса или None в случае ошибки.
    """
    conn = None
    try:
        conn = sqlite3.connect(DB_NAME, timeout=10) # Увеличиваем таймаут
        conn.execute("PRAGMA journal_mode=WAL;") # Улучшает параллельный доступ
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
    """Инициализирует (создает, если не существует) таблицы базы данных."""
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
    """
    Выполняет пакетную вставку записей в таблицу accepted_scooters.
    :param records_data: Список кортежей с данными для вставки.
    """
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
    """Асинхронно записывает пакет данных в БД."""
    loop = asyncio.get_running_loop()
    global db_executor
    await loop.run_in_executor(db_executor, insert_batch_records, records_data)

async def db_fetch_all(query: str, params: tuple = ()):
    """Асинхронно выполняет запрос на выборку всех данных из БД."""
    loop = asyncio.get_running_loop()
    global db_executor
    return await loop.run_in_executor(db_executor, run_db_query, query, params, 'all')

# --- Обработчики команд и сообщений ---
@dp.message_handler(IsAllowedChatFilter(), commands="start")
async def command_start_handler(message: types.Message):
    """Обработчик команды /start."""
    allowed_chats_info = ', '.join(map(str, ALLOWED_CHAT_IDS)) if ALLOWED_CHAT_IDS else "не указаны"
    response = (
        f"Привет, {message.from_user.full_name}! Я бот для приёма самокатов.\n\n"
        f"Просто отправь мне **номер самоката текстом** или **фотографию с номером в подписи**.\n"
        f"Для пакетного приёма используй формат: `сервис количество` (например, `Яндекс 10`, `y 5`, `Whoosh 15`, `w 20`, `Jet 8`, `j 3`).\n\n"
        f"Я работаю в группах с ID: `{allowed_chats_info}` и в личных сообщениях с администраторами.\n"
        f"Твой ID чата: `{message.chat.id}`"
    )
    await message.answer(response, parse_mode="Markdown")

@dp.message_handler(IsAllowedChatFilter(), commands="batch_accept")
async def batch_accept_handler(message: types.Message):
    """Обработчик команды /batch_accept для пакетного приема самокатов."""
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
        if not (0 < quantity <= 200): # Ограничение на количество в пакетном приеме
            raise ValueError
    except ValueError:
        await message.reply("Количество должно быть числом от 1 до 200.")
        return

    user = message.from_user
    now_localized_str = datetime.datetime.now(TIMEZONE).strftime("%Y-%m-%d %H:%M:%S")

    records_to_insert = [
        (
            f"{service.upper()}_BATCH_{datetime.datetime.now().strftime('%H%M%S%f')}_{i+1}", # Уникальный номер для каждой пакетной записи
            service, user.id, user.username, user.full_name, now_localized_str, message.chat.id
        ) for i in range(quantity)
    ]

    await db_write_batch(records_to_insert)

    user_mention = types.User.get_mention(user)
    await message.reply(f"{user_mention}, принято {quantity} самокатов сервиса <b>{service}</b>.")

# --- Функции для определения временных диапазонов смен ---
def get_shift_time_range_for_report(shift_type: str):
    """
    Определяет время начала и конца смены для формирования отчета.
    :param shift_type: Тип смены ('morning' или 'evening').
    :return: Кортеж (start_time, end_time, shift_name).
    """
    now = datetime.datetime.now(TIMEZONE)
    today = now.date()
    
    if shift_type == 'morning':
        start_time = TIMEZONE.localize(datetime.datetime.combine(today, datetime.time(7, 0, 0)))
        end_time = TIMEZONE.localize(datetime.datetime.combine(today, datetime.time(15, 0, 0)))
        shift_name = "утреннюю смену"
    elif shift_type == 'evening':
        # Вечерняя смена считается с 15:00 текущего дня до 04:00 следующего дня
        evening_start_actual = TIMEZONE.localize(datetime.datetime.combine(today, datetime.time(15, 0, 0)))
        evening_end_extended = TIMEZONE.localize(datetime.datetime.combine(today + datetime.timedelta(days=1), datetime.time(4, 0, 0)))
        
        start_time = evening_start_actual
        end_time = evening_end_extended
        shift_name = "вечернюю смену (с учетом ночных часов)"
    else:
        return None, None, None # Неизвестный тип смены
        
    return start_time, end_time, shift_name

def get_shift_time_range():
    """
    Определяет текущую смену и ее временной диапазон относительно текущего времени.
    Используется для команды /today_stats.
    :return: Кортеж (start_time, end_time, shift_name).
    """
    now = datetime.datetime.now(TIMEZONE)
    today = now.date()

    morning_shift_start = TIMEZONE.localize(datetime.datetime.combine(today, datetime.time(7, 0, 0)))
    morning_shift_end = TIMEZONE.localize(datetime.datetime.combine(today, datetime.time(15, 0, 0)))
    evening_shift_start = TIMEZONE.localize(datetime.datetime.combine(today, datetime.time(15, 0, 0)))
    evening_shift_end = TIMEZONE.localize(datetime.datetime.combine(today, datetime.time(23, 0, 0))) # Основной конец вечерней смены по дневным часам

    if morning_shift_start <= now < morning_shift_end:
        return morning_shift_start, morning_shift_end, "утреннюю смену"
    elif evening_shift_start <= now < evening_shift_end:
        return evening_shift_start, evening_shift_end, "вечернюю смену"
    else:
        # Проверяем, если текущее время относится к ночной части вечерней смены (с 00:00 до 04:00)
        prev_day = today - datetime.timedelta(days=1)
        night_cutoff_current_day = TIMEZONE.localize(datetime.datetime.combine(today, datetime.time(4, 0, 0)))

        if TIMEZONE.localize(datetime.datetime.combine(today, datetime.time(0,0,0))) <= now < night_cutoff_current_day:
            prev_evening_shift_start = TIMEZONE.localize(datetime.datetime.combine(prev_day, datetime.time(15, 0, 0)))
            return prev_evening_shift_start, night_cutoff_current_day, "вечернюю смену (с учетом ночных часов)"
        else:
            # Если время после 23:00 и до 00:00 (следующего дня, не попавшее в ночной диапазон)
            # или до 07:00 (утро, до начала утренней смены)
            if now.hour >= 23: # Если уже после 23:00, но до полуночи
                return evening_shift_start, evening_shift_end, "вечернюю смену"
            return morning_shift_start, morning_shift_end, "утреннюю смену (еще не началась)"


# --- Обработчик статистики за текущую смену ---
@dp.message_handler(IsAdminFilter(), commands="today_stats")
async def today_stats_handler(message: types.Message):
    """
    Отправляет статистику по принятым самокатам за текущую смену.
    Доступно только администраторам.
    """
    start_time, end_time, shift_name = get_shift_time_range()
    
    start_str = start_time.strftime("%Y-%m-%d %H:%M:%S")
    end_str = end_time.strftime("%Y-%m-%d %H:%M:%S")

    query = "SELECT service, accepted_by_user_id, accepted_by_username, accepted_by_fullname FROM accepted_scooters WHERE timestamp BETWEEN ? AND ?"
    records = await db_fetch_all(query, (start_str, end_str))

    if not records:
        await message.answer(f"За {shift_name} пока ничего не принято.")
        return

    user_stats = defaultdict(lambda: defaultdict(int))
    user_info = {}
    service_totals = defaultdict(int)

    for service, user_id, username, fullname in records:
        user_stats[user_id][service] += 1
        service_totals[service] += 1
        if user_id not in user_info:
            user_info[user_id] = f"@{username}" if username else fullname

    response_parts = [f"<b>Статистика за {shift_name} ({start_time.strftime('%H:%M')} - {end_time.strftime('%H:%M')}):</b>"]
    total_all_users = 0

    for user_id, services in user_stats.items():
        user_total = sum(services.values())
        total_all_users += user_total
        response_parts.append(f"\n<b>{user_info[user_id]}</b> - всего: {user_total} шт.")
        for service, count in sorted(services.items()):
            response_parts.append(f"  - {service}: {count} шт.")

    response_parts.append("\n<b>Итог по сервисам:</b>")
    for service, count in sorted(service_totals.items()):
        response_parts.append(f"<b>{service}</b>: {count} шт.")

    response_parts.append(f"\n<b>Общий итог за {shift_name}: {total_all_users} шт.</b>")
    
    MESSAGE_LIMIT = 4000 # Лимит на длину сообщения в Telegram
    current_message_buffer = []
    
    for part in response_parts:
        # Проверяем, не превысит ли добавление текущей части лимит
        if len('\n'.join(current_message_buffer)) + len(part) + (1 if current_message_buffer else 0) > MESSAGE_LIMIT:
            if current_message_buffer: # Если буфер не пуст, отправляем его
                await message.answer("\n".join(current_message_buffer))
                current_message_buffer = [] # Очищаем буфер
        current_message_buffer.append(part)
    
    if current_message_buffer: # Отправляем оставшиеся части
        await message.answer("\n".join(current_message_buffer))


# --- Обработчик экспорта в Excel ---
@dp.message_handler(IsAdminFilter(), commands=["export_today_excel", "export_all_excel"])
async def export_excel_handler(message: types.Message):
    """
    Экспортирует данные о принятых самокатах в Excel файл.
    Доступно только администраторам.
    """
    is_today_shift = message.get_command() == '/export_today_excel'
    
    await message.answer(f"Формирую отчет...")

    query = "SELECT id, scooter_number, service, accepted_by_user_id, accepted_by_username, accepted_by_fullname, timestamp, chat_id FROM accepted_scooters"
    
    if is_today_shift:
        start_time, end_time, shift_name = get_shift_time_range()
        start_str = start_time.strftime("%Y-%m-%d %H:%M:%S")
        end_str = end_time.strftime("%Y-%m-%d %H:%M:%S")
        query += " WHERE timestamp BETWEEN ? AND ?"
        records = await db_fetch_all(query, (start_str, end_str))
        date_filter_text = f" за {shift_name}"
    else:
        query += " ORDER BY timestamp DESC" # Для отчета за все время сортируем по убыванию даты
        records = await db_fetch_all(query)
        date_filter_text = " за все время"

    if not records:
        await message.answer(f"Нет данных для экспорта{date_filter_text}.")
        logging.info(f"Нет данных для экспорта отчета{date_filter_text}.")
        return

    try:
        excel_file = create_excel_report(records)
        report_type = "shift" if is_today_shift else "full"
        filename = f"report_{report_type}_{datetime.date.today().isoformat()}.xlsx"
        
        logging.info(f"Попытка отправить Excel файл: {filename}, размер: {excel_file.getbuffer().nbytes} байт.")
        await bot.send_document(message.chat.id, types.InputFile(excel_file, filename=filename), caption=f"Ваш отчет{date_filter_text} готов.")
    except Exception as e:
        logging.error(f"Ошибка при отправке Excel файла: {e}", exc_info=True)
        await message.answer("Произошла ошибка при отправке отчета. Пожалуйста, свяжитесь с администратором.")

# --- Функция создания Excel отчета (с последними изменениями для удаления лишних листов) ---
def create_excel_report(records: List[Tuple]) -> BytesIO:
    """
    Создает Excel отчет с отдельными листами для каждого пользователя.
    :param records: Список кортежей с данными из БД.
    :return: Объект BytesIO, содержащий Excel файл.
    """
    wb = Workbook()
    
    # !!! КЛЮЧЕВОЕ ИЗМЕНЕНИЕ: Удаляем активный лист, который создается по умолчанию openpyxl
    # Перед тем как создавать свои листы, убедимся, что начальный пустой лист удален.
    # Это предотвратит появление листа "Sheet" или "Sheet1"
    if 'Sheet' in wb.sheetnames:
        del wb['Sheet']
    # Если default-лист был 'Sheet1' (например, в более старых версиях openpyxl)
    elif 'Sheet1' in wb.sheetnames:
        del wb['Sheet1']
    # Если после всех операций вдруг остался какой-то активный лист, удаляем его
    if wb.active:
        wb.remove(wb.active)

    header_font = Font(bold=True)

    # --- Отдельные листы для каждого пользователя ---
    user_records = defaultdict(list)
    user_info_for_sheets = {}

    for record in records:
        user_id = record[3]
        username = record[4]
        fullname = record[5]
        # Предпочтение: Полное имя, затем никнейм, затем ID
        display_name = fullname if fullname else (f"@{username}" if username else f"ID_{user_id}")
        
        user_records[user_id].append(record)
        if user_id not in user_info_for_sheets:
            user_info_for_sheets[user_id] = display_name

    # Сортируем пользователей по их отображаемому имени для последовательности листов
    sorted_user_ids = sorted(user_records.keys(), key=lambda user_id: user_info_for_sheets[user_id].lower())

    # Заголовки, которые будут на каждом листе пользователя
    # ID записи, Номер Самоката, Сервис, Время Принятия, ID Чата
    user_sheet_headers = ["ID записи", "Номер Самоката", "Сервис", "Время Принятия", "ID Чата"] 

    # Если данных нет, или нет пользователей (крайне маловероятно после проверки в экспортере)
    # нужно создать хотя бы один пустой лист, иначе openpyxl выдаст ошибку при сохранении.
    if not sorted_user_ids:
        ws_empty = wb.create_sheet(title="Нет данных", index=0)
        ws_empty.append(["Нет записей для создания отчетов по пользователям."])
        
    for user_id in sorted_user_ids:
        user_display_name = user_info_for_sheets[user_id]
        
        # Очистка и формирование имени листа
        # Лимит на имя листа в Excel - 31 символ
        # Недопустимые символы: \ / : * ? " < > |
        sheet_name_raw = user_display_name # Начинаем с полного имени/никнейма
        
        # Удаляем запрещенные символы
        invalid_chars = re.compile(r'[\\/:*?"<>|]')
        sheet_name = invalid_chars.sub('', sheet_name_raw)
        
        # Обрезаем до 31 символа, если необходимо
        if len(sheet_name) > 31:
            sheet_name = sheet_name[:31]
            
        # Если после очистки и обрезки имя пустое, слишком короткое или неподходящее
        if not sheet_name.strip() or len(sheet_name.strip()) < 3: # Проверяем на пустые строки или очень короткие
             sheet_name = f"ID_{user_id}" # Используем ID как запасной вариант

        # Убедимся, что имя листа уникально
        original_sheet_name = sheet_name
        counter = 1
        while sheet_name in wb.sheetnames:
            # Если имя уже занято, добавляем счетчик
            # Стараемся уместиться в 31 символ
            suffix = f"_{counter}"
            sheet_name = f"{original_sheet_name[:31 - len(suffix)]}{suffix}"
            counter += 1

        ws_user = wb.create_sheet(title=sheet_name) # Создаем новый лист для пользователя
        
        # Записываем заголовки
        ws_user.append(user_sheet_headers)
        for cell in ws_user[1]:
            cell.font = header_font

        current_user_total = 0
        user_service_breakdown = defaultdict(int) # Для статистики по сервисам на листе пользователя

        # Записи для текущего пользователя
        # Структура record: ID, Номер Самоката, Сервис, ID Пользователя, Ник, Полное имя, Время Принятия, ID Чата
        # Для листа пользователя нам нужны: ID записи (0), Номер Самоката (1), Сервис (2), Время Принятия (6), ID Чата (7)
        for record in user_records[user_id]:
            row_to_add = [record[0], record[1], record[2], record[6], record[7]]
            ws_user.append(row_to_add)
            current_user_total += 1
            user_service_breakdown[record[2]] += 1 # Считаем по сервисам для этого пользователя

        # Добавляем итоговую информацию в конце листа пользователя
        ws_user.append([]) # Пустая строка для разделения
        
        # Статистика по сервисам
        ws_user.append(["Статистика по сервисам:"])
        ws_user.cell(row=ws_user.max_row, column=1).font = Font(bold=True)
        # Объединяем ячейки для заголовка статистики, если нужно, чтобы он был по центру
        # Количество столбцов для объединения равно длине user_sheet_headers
        ws_user.merge_cells(start_row=ws_user.max_row, start_column=1, end_row=ws_user.max_row, end_column=len(user_sheet_headers))
        ws_user.cell(row=ws_user.max_row, column=1).alignment = Alignment(horizontal='center')

        for service, count in sorted(user_service_breakdown.items()):
            ws_user.append([service, count]) # Начинаем с первой колонки

        ws_user.append([]) # Еще одна пустая строка
        
        # Общий итог
        ws_user.append(["Всего принято:", current_user_total])
        # Объединяем ячейки "Всего принято:"
        # Объединяем ячейки до предпоследней колонки, чтобы последняя осталась для значения total
        ws_user.merge_cells(start_row=ws_user.max_row, start_column=1, end_row=ws_user.max_row, end_column=len(user_sheet_headers) - 1)
        ws_user.cell(row=ws_user.max_row, column=1).font = Font(bold=True)
        ws_user.cell(row=ws_user.max_row, column=1).alignment = Alignment(horizontal='right')
        ws_user.cell(row=ws_user.max_row, column=len(user_sheet_headers)).font = Font(bold=True)
        ws_user.cell(row=ws_user.max_row, column=len(user_sheet_headers)).alignment = Alignment(horizontal='center')


        # Автонастройка ширины столбцов на листе пользователя
        # Учитываем только те столбцы, которые есть в user_sheet_headers
        for col_idx in range(len(user_sheet_headers)):
            max_length = 0
            column_letter = get_column_letter(col_idx + 1)
            for cell in ws_user[column_letter]: # Итерируемся по ячейкам конкретной колонки
                try:
                    if cell.value:
                        length = len(str(cell.value))
                        if length > max_length:
                            max_length = length
                except:
                    pass
            adjusted_width = (max_length + 2) * 1.2
            ws_user.column_dimensions[column_letter].width = adjusted_width
            
    buffer = BytesIO()
    wb.save(buffer)
    buffer.seek(0)
    return buffer

# --- Функции обработки входящих сообщений (текст и фото) ---
async def process_scooter_text(message: types.Message, text_to_process: str):
    """
    Обрабатывает текстовые сообщения на предмет номеров самокатов или пакетного приема.
    :param message: Объект сообщения Telegram.
    :param text_to_process: Текст для обработки (может быть подписью фото).
    :return: True, если что-то было принято, False в противном случае.
    """
    user = message.from_user
    now_localized_str = datetime.datetime.now(TIMEZONE).strftime("%Y-%m-%d %H:%M:%S")

    records_to_insert = []
    accepted_summary = defaultdict(int)
    
    text_for_numbers = text_to_process

    # Обработка пакетного приема через текст сообщения (например, "yandex 10")
    batch_matches = BATCH_QUANTITY_PATTERN.findall(text_to_process)
    if batch_matches:
        for service_raw, quantity_str in batch_matches:
            service = SERVICE_ALIASES.get(service_raw.lower())
            try:
                quantity = int(quantity_str)
                if service and 0 < quantity <= 200: # Лимит на пакетный прием
                    for i in range(quantity):
                        # Генерируем уникальный номер для каждой записи пакетного приема
                        placeholder_number = f"{service.upper()}_BATCH_{datetime.datetime.now().strftime('%H%M%S%f')}_{i+1}" 
                        records_to_insert.append((placeholder_number, service, user.id, user.username, user.full_name, now_localized_str, message.chat.id))
                    accepted_summary[service] += quantity
            except (ValueError, TypeError):
                # Если количество не число или другие ошибки, игнорируем этот пакет
                continue
        # Удаляем обработанные пакетные команды из текста, чтобы не парсить их как отдельные номера самокатов
        text_for_numbers = BATCH_QUANTITY_PATTERN.sub('', text_to_process)

    # Обработка одиночных номеров самокатов
    patterns = {
        "Яндекс": YANDEX_SCOOTER_PATTERN,
        "Whoosh": WOOSH_SCOOTER_PATTERN,
        "Jet": JET_SCOOTER_PATTERN
    }
    
    processed_numbers = set() # Чтобы избежать дубликатов из одного сообщения

    for service, pattern in patterns.items():
        numbers = pattern.findall(text_for_numbers)
        for num in numbers:
            clean_num = num.replace('-', '') if service == "Jet" else num.upper()
            
            if clean_num in processed_numbers:
                continue # Пропускаем уже обработанные номера
            
            records_to_insert.append((clean_num, service, user.id, user.username, user.full_name, now_localized_str, message.chat.id))
            accepted_summary[service] += 1
            processed_numbers.add(clean_num)

    if not records_to_insert:
        # Если ничего не было найдено ни в пакетном, ни в одиночном режиме
        # Отправляем сообщение об ошибке только здесь, чтобы избежать спама
        await message.reply("Не удалось распознать номера самокатов или формат пакетного приема в вашем сообщении. Пожалуйста, убедитесь, что номера корректны или используйте формат `сервис количество`.")
        return False

    await db_write_batch(records_to_insert)

    response_parts = []
    user_mention = types.User.get_mention(user)
    total_accepted = sum(accepted_summary.values())
    response_parts.append(f"{user_mention}, принято {total_accepted} шт.:")

    for service, count in sorted(accepted_summary.items()):
        if count > 0:
            response_parts.append(f"  - <b>{service}</b>: {count} шт.")

    await message.reply("\n".join(response_parts))
    return True

# --- Основной обработчик текстовых сообщений (не команды) ---
# regexp=r'^(?!/).*$' гарантирует, что этот обработчик не будет срабатывать на команды (начинающиеся с '/')
@dp.message_handler(IsAllowedChatFilter(), content_types=types.ContentTypes.TEXT, regexp=r'^(?!/).*$')
async def handle_text_messages(message: types.Message):
    """
    Обрабатывает текстовые сообщения пользователя, которые не являются командами.
    """
    await process_scooter_text(message, message.text)

# --- Основной обработчик фото с подписью ---
@dp.message_handler(IsAllowedChatFilter(), content_types=types.ContentTypes.PHOTO)
async def handle_photo_messages(message: types.Message):
    """
    Обрабатывает фотографии с подписями. Подпись передается для распознавания номеров.
    """
    if message.caption:
        await process_scooter_text(message, message.caption)
    else:
        await message.reply("Пожалуйста, добавьте номер самоката в подпись к фотографии.")

# --- Обработчик для всех остальных типов контента (catch-all) ---
# Этот обработчик должен быть ПОСЛЕ всех остальных, более специфических.
@dp.message_handler(IsAllowedChatFilter(), content_types=types.ContentTypes.ANY)
async def handle_unsupported_content(message: types.Message):
    """
    Обрабатывает любые сообщения, которые не были перехвачены другими, более специфическими обработчиками.
    Это помогает избежать спама ответами на неподдерживаемый контент.
    """
    # Если сообщение является командой (даже если неизвестной), просто игнорируем его,
    # чтобы не отвечать на каждую неправильно набранную команду.
    if message.text and message.text.startswith('/'):
        return

    # Если это не фото и не текст, и это не команда, значит это действительно неподдерживаемый контент (стикер, видео, голосовое и т.д.)
    if not (message.photo or message.text): 
        await message.reply("Извините, я могу обрабатывать только текстовые сообщения и фотографии (с подписями). "
                            "Видео, документы и другие файлы я не поддерживаю.")
        return
    
    # Если это был просто текст, который не распознался (и не был командой),
    # мы уже ответили на него в process_scooter_text (если records_to_insert был пуст).
    # Здесь мы просто ничего не делаем, чтобы избежать дублирования ответов или спама на "мусорный" текст.
    pass


# --- Функции для планировщика задач (автоматические отчеты) ---
async def send_scheduled_report(shift_type: str):
    """
    Функция для отправки автоматического отчета по расписанию.
    Отправляет Excel файл с данными за указанную смену в REPORT_CHAT_IDS.
    :param shift_type: Тип смены ('morning' или 'evening').
    """
    logging.info(f"Запуск отправки автоматического отчета для {shift_type} смены.")
    
    start_time, end_time, shift_name = get_shift_time_range_for_report(shift_type)
    
    if not start_time or not end_time:
        logging.error(f"Не удалось определить временной диапазон для отчета '{shift_type}' смены. Проверьте TIMEZONE или логику get_shift_time_range_for_report.")
        return

    start_str = start_time.strftime("%Y-%m-%d %H:%M:%S")
    end_str = end_time.strftime("%Y-%m-%d %H:%M:%S")

    query = "SELECT id, scooter_number, service, accepted_by_user_id, accepted_by_username, accepted_by_fullname, timestamp, chat_id FROM accepted_scooters WHERE timestamp BETWEEN ? AND ?"
    records = await db_fetch_all(query, (start_str, end_str))

    if not records:
        message_text = f"Отчет за {shift_name} ({start_time.strftime('%d.%m %H:%M')} - {end_time.strftime('%d.%m %H:%M')}): За смену ничего не принято."
        for chat_id in REPORT_CHAT_IDS:
            try:
                await bot.send_message(chat_id, message_text)
                logging.info(f"Отправлено уведомление об отсутствии данных за {shift_name} в чат {chat_id}.")
            except Exception as e:
                logging.error(f"Ошибка отправки уведомления в чат {chat_id}: {e}")
        return

    try:
        excel_file = create_excel_report(records)
        report_type_filename = "morning_shift" if shift_type == 'morning' else "evening_shift"
        filename = f"report_{report_type_filename}_{start_time.strftime('%Y%m%d')}.xlsx"
        caption = f"Ежедневный отчет за {shift_name} ({start_time.strftime('%d.%m %H:%M')} - {end_time.strftime('%d.%m %H:%M')})"
        
        for chat_id in REPORT_CHAT_IDS:
            try:
                excel_file.seek(0) # Важно: сбрасываем указатель на начало файла для каждого send_document
                await bot.send_document(chat_id, types.InputFile(excel_file, filename=filename), caption=caption)
                logging.info(f"Отправлен Excel отчет за {shift_name} в чат {chat_id}.")
            except Exception as e:
                logging.error(f"Ошибка отправки Excel файла в чат {chat_id}: {e}", exc_info=True)

    except Exception as e:
        logging.error(f"Произошла общая ошибка при формировании или отправке Excel отчета: {e}", exc_info=True)
        for admin_id in ADMIN_IDS:
            try:
                await bot.send_message(admin_id, f"Ошибка при формировании/отправке отчета за {shift_name}: {e}")
            except Exception as err:
                logging.error(f"Не удалось отправить уведомление об ошибке администратору {admin_id}: {err}")

# --- Функции запуска и остановки бота ---
async def on_startup(dispatcher: Dispatcher):
    """Действия при запуске бота: инициализация БД, планировщика, установка команд."""
    global db_executor
    # Инициализируем ThreadPoolExecutor для асинхронных операций с БД
    db_executor = ThreadPoolExecutor(max_workers=5)
    
    # Запускаем инициализацию БД в отдельном потоке, чтобы не блокировать основной цикл
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(db_executor, init_db)
    
    global scheduler
    # Инициализируем APScheduler
    scheduler = AsyncIOScheduler(timezone=str(TIMEZONE))
    
    # Планируем задачи для автоматических отчетов
    # Отчет за утреннюю смену (7:00-15:00) отправляется в 15:00
    scheduler.add_job(send_scheduled_report, 'cron', hour=15, minute=0, timezone=str(TIMEZONE), args=['morning'])
    logging.info("Задача для отправки утреннего отчета (в 15:00) запланирована.")
    
    # Отчет за вечернюю смену (15:00-04:00) отправляется в 04:00 следующего дня
    # Для этого в get_shift_time_range_for_report(shift_type='evening') указан соответствующий диапазон
    scheduler.add_job(send_scheduled_report, 'cron', hour=4, minute=0, timezone=str(TIMEZONE), args=['evening'])
    logging.info("Задача для отправки вечернего отчета (в 04:00) запланирована.")
    
    scheduler.start()
    logging.info("APScheduler запущен.")
    
    # Установка команд для бота
    admin_commands = [
        types.BotCommand(command="start", description="Начало работы"),
        types.BotCommand(command="today_stats", description="Статистика за текущую смену"),
        types.BotCommand(command="export_today_excel", description="Экспорт Excel за текущую смену"),
        types.BotCommand(command="export_all_excel", description="Экспорт Excel за все время"),
        types.BotCommand(command="batch_accept", description="Пакетный прием (сервис кол-во)"),
    ]
    await dispatcher.bot.set_my_commands(admin_commands)
    logging.info("Бот запущен и команды установлены.")


async def on_shutdown(dispatcher: Dispatcher):
    """Действия при остановке бота: остановка пула потоков БД и планировщика."""
    global db_executor
    if db_executor:
        db_executor.shutdown(wait=True)
    
    global scheduler
    if scheduler:
        scheduler.shutdown()
        logging.info("APScheduler остановлен.")
        
    logging.info("Пул потоков БД остановлен.")
    logging.info("Бот остановлен.")

if __name__ == "__main__":
    # Запускаем бота, пропуская уже обработанные обновления во время простоя (skip_updates=True)
    executor.start_polling(dp, on_startup=on_startup, on_shutdown=on_shutdown, skip_updates=True)
