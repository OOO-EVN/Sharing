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
from aiogram.utils.exceptions import MessageIsTooLong # Импортируем исключение

from dotenv import load_dotenv
from openpyxl import Workbook
from openpyxl.styles import Font

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# Загрузка переменных окружения из .env файла
load_dotenv()

# Получение токена бота и ID администраторов/разрешенных чатов
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

# Константы для работы бота
DB_NAME = 'scooters.db'
TIMEZONE = pytz.timezone('Asia/Almaty') # Ваша таймзона UTC+5

# Регулярные выражения для определения номеров самокатов по сервисам
YANDEX_SCOOTER_PATTERN = re.compile(r'\b(\d{8})\b')
WOOSH_SCOOTER_PATTERN = re.compile(r'\b([A-ZА-Я]{2}\d{4})\b', re.IGNORECASE)
JET_SCOOTER_PATTERN = re.compile(r'\b(\d{3}-?\d{3})\b')

# Регулярное выражение и алиасы для пакетного приема самокатов
BATCH_QUANTITY_PATTERN = re.compile(r'\b(whoosh|jet|yandex|вуш|джет|яндекс|w|j|y)\s+(\d+)\b', re.IGNORECASE)
SERVICE_ALIASES = {
    "yandex": "Яндекс", "яндекс": "Яндекс", "y": "Яндекс",
    "whoosh": "Whoosh", "вуш": "Whoosh", "w": "Whoosh",
    "jet": "Jet", "джет": "Jet", "j": "Jet"
}
SERVICE_MAP = {"yandex": "Яндекс", "whoosh": "Whoosh", "jet": "Jet"} # На случай если понадобится полное имя сервиса

# Инициализация бота и диспетчера
bot = Bot(token=BOT_TOKEN, parse_mode="HTML")
storage = MemoryStorage()
dp = Dispatcher(bot, storage=storage)

# Инициализация пула потоков для работы с базой данных
db_executor = None

# --- Фильтры для контроля доступа ---
class IsAdminFilter(BoundFilter):
    async def check(self, message: types.Message) -> bool:
        return message.from_user.id in ADMIN_IDS

class IsAllowedChatFilter(BoundFilter):
    async def check(self, message: types.Message) -> bool:
        # Разрешаем администраторам писать в личку
        if message.chat.type == 'private' and message.from_user.id in ADMIN_IDS:
            return True
        # Разрешаем сообщения в указанных группах
        if message.chat.type in ['group', 'supergroup'] and message.chat.id in ALLOWED_CHAT_IDS:
            return True
        # Логируем попытки неразрешенного доступа
        logging.warning(f"Сообщение от {message.from_user.id} в чате {message.chat.id} было заблокировано фильтром.")
        return False

# --- Функции для работы с базой данных ---
def run_db_query(query: str, params: tuple = (), fetch: str = None):
    conn = None
    try:
        conn = sqlite3.connect(DB_NAME, timeout=10)
        conn.execute("PRAGMA journal_mode=WAL;") # Включаем WAL режим для лучшей производительности и конкурентности
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
    global db_executor
    await loop.run_in_executor(db_executor, insert_batch_records, records_data)

async def db_fetch_all(query: str, params: tuple = ()):
    loop = asyncio.get_running_loop()
    global db_executor
    return await loop.run_in_executor(db_executor, run_db_query, query, params, 'all')

# --- Команды бота ---
@dp.message_handler(IsAllowedChatFilter(), commands="start")
async def command_start_handler(message: types.Message):
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
            f"{service.upper()}_BATCH_{i+1}", # Создаем уникальный номер для каждой записи в пакете
            service, user.id, user.username, user.full_name, now_localized_str, message.chat.id
        ) for i in range(quantity)
    ]

    await db_write_batch(records_to_insert)

    user_mention = types.User.get_mention(user)
    await message.reply(f"{user_mention}, принято {quantity} самокатов сервиса <b>{service}</b>.")

def get_shift_time_range():
    """
    Определяет начало и конец текущей смены (утренней или вечерней) в Almaty (UTC+5).
    Утренняя смена: 07:00 - 15:00
    Вечерняя смена: 15:00 - 23:00
    Ночной период (с 23:00 до 04:00 следующего дня) относится к предыдущей вечерней смене.
    Период с 04:00 до 07:00 считается межсменным, но для статистики возвращает диапазон предстоящей утренней смены.
    """
    now = datetime.datetime.now(TIMEZONE)
    today = now.date()

    morning_shift_start = TIMEZONE.localize(datetime.datetime.combine(today, datetime.time(7, 0, 0)))
    morning_shift_end = TIMEZONE.localize(datetime.datetime.combine(today, datetime.time(15, 0, 0)))
    evening_shift_start = TIMEZONE.localize(datetime.datetime.combine(today, datetime.time(15, 0, 0)))
    evening_shift_end = TIMEZONE.localize(datetime.datetime.combine(today, datetime.time(23, 0, 0)))

    # Если текущее время между 07:00 и 15:00 (утренняя смена)
    if morning_shift_start <= now < morning_shift_end:
        return morning_shift_start, morning_shift_end, "утреннюю смену"
    # Если текущее время между 15:00 и 23:00 (вечерняя смена)
    elif evening_shift_start <= now < evening_shift_end:
        return evening_shift_start, evening_shift_end, "вечернюю смену"
    # Если сейчас ночь (после 23:00 текущего дня или до 07:00 следующего дня)
    else:
        # Определяем ночной интервал, который относится к предыдущей вечерней смене
        night_start_prev_day = TIMEZONE.localize(datetime.datetime.combine(today - datetime.timedelta(days=1), datetime.time(23, 0, 0)))
        night_end_current_day = TIMEZONE.localize(datetime.datetime.combine(today, datetime.time(4, 0, 0)))

        # Если сейчас между 23:00 текущего дня и 00:00 следующего дня (включительно)
        if now.hour >= 23:
            # Это все еще часть "текущей" вечерней смены (до ее официального конца в 23:00)
            return evening_shift_start, evening_shift_end, "вечернюю смену"
        # Если сейчас между 00:00 и 04:00 текущего дня (ночные часы, относящиеся к ВЧЕРАШНЕЙ вечерней смене)
        elif TIMEZONE.localize(datetime.datetime.combine(today, datetime.time(0,0,0))) <= now < night_end_current_day:
            prev_evening_shift_start = TIMEZONE.localize(datetime.datetime.combine(today - datetime.timedelta(days=1), datetime.time(15, 0, 0)))
            return prev_evening_shift_start, night_end_current_day, "вечернюю смену (с учетом ночных часов)"
        # Если время с 04:00 до 07:00 (межсменное время, до начала новой утренней)
        else:
            # Для команды /today_stats в этот период возвращаем диапазон предстоящей утренней смены
            # (которая на данный момент будет пустой)
            return morning_shift_start, morning_shift_end, "утреннюю смену (еще не началась)"


@dp.message_handler(IsAdminFilter(), commands="today_stats")
async def today_stats_handler(message: types.Message):
    start_time, end_time, shift_name = get_shift_time_range()
    
    # Форматируем времена для запроса к БД
    start_str = start_time.strftime("%Y-%m-%d %H:%M:%S")
    end_str = end_time.strftime("%Y-%m-%d %H:%M:%S")

    # Изменяем запрос, чтобы использовать диапазон времени
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
    
    # --- БЛОК ДЛЯ РАЗДЕЛЕНИЯ СООБЩЕНИЯ ---
    MESSAGE_LIMIT = 4000  # Максимальная длина сообщения в Telegram - 4096 символов. Оставляем запас.
    current_message_buffer = []
    
    for part in response_parts:
        # Проверяем, если добавление текущей части (с учетом новой строки) превысит лимит
        # len('\n'.join(current_message_buffer)) - длина уже накопленных строк
        # len(part) - длина текущей строки
        # (1 if current_message_buffer else 0) - добавляем 1 за '\n', если буфер не пуст
        if len('\n'.join(current_message_buffer)) + len(part) + (1 if current_message_buffer else 0) > MESSAGE_LIMIT:
            # Отправляем текущий буфер, если он не пуст
            if current_message_buffer:
                await message.answer("\n".join(current_message_buffer))
                current_message_buffer = [] # Очищаем буфер
        
        current_message_buffer.append(part)
    
    # Отправляем оставшиеся части, если они есть в буфере
    if current_message_buffer:
        await message.answer("\n".join(current_message_buffer))
    # --- КОНЕЦ БЛОКА РАЗДЕЛЕНИЯ СООБЩЕНИЯ ---


@dp.message_handler(IsAdminFilter(), commands=["export_today_excel", "export_all_excel"])
async def export_excel_handler(message: types.Message):
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
        query += " ORDER BY timestamp DESC" # Сортируем по дате для полного отчета
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
        # Примечание: если timestamp в БД хранится как строка, openpyxl обычно хорошо справляется.
        # Если нужна дата/время как объект для Excel, можно добавить:
        # row_list = list(row)
        # row_list[6] = datetime.datetime.strptime(row_list[6], "%Y-%m-%d %H:%M:%S") # если нужно преобразовать
        # ws.append(row_list)
        ws.append(row)

    # Автонастройка ширины столбцов на листе "Данные"
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
    user_info_map = {} # Для хранения ников/полных имен, чтобы использовать их для сортировки

    for record in records:
        service = record[2]
        user_id = record[3]
        username = record[4]
        fullname = record[5]
        
        # Используем полное имя, если есть, иначе ник, иначе ID
        display_name = fullname if fullname else (f"@{username}" if username else f"ID: {user_id}")
        
        user_service_counts[user_id][service] += 1
        if user_id not in user_info_map:
            user_info_map[user_id] = display_name

    # Сортируем сначала по отображаемому имени пользователя (без учета регистра), затем по сервису
    sorted_user_ids = sorted(user_service_counts.keys(), key=lambda user_id: user_info_map[user_id].lower())

    for user_id in sorted_user_ids:
        user_display_name = user_info_map[user_id]
        services_data = user_service_counts[user_id]
        
        # Добавляем строку-разделитель или заголовок для каждого пользователя (опционально, сейчас просто подряд)
        # Если вы хотите "пустую" строку между пользователями, можно добавить:
        # if ws_summary.max_row > 1: # Пропускаем для первой группы
        #     ws_summary.append(["", "", ""]) # Пустая строка как разделитель
        
        for service, count in sorted(services_data.items()):
            ws_summary.append([user_display_name, service, count])
    
    # Автонастройка ширины столбцов на листе "Сводка"
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

# --- Унифицированная функция для обработки текста из любого источника (сообщения или подписи) ---
async def process_scooter_text(message: types.Message, text_to_process: str):
    user = message.from_user
    now_localized_str = datetime.datetime.now(TIMEZONE).strftime("%Y-%m-%d %H:%M:%S")

    records_to_insert = []
    accepted_summary = defaultdict(int)
    
    # Копия текста, из которой будут удаляться уже обработанные пакетные записи
    text_for_numbers = text_to_process

    # Поиск и обработка пакетных записей
    batch_matches = BATCH_QUANTITY_PATTERN.findall(text_to_process)
    if batch_matches:
        for service_raw, quantity_str in batch_matches:
            service = SERVICE_ALIASES.get(service_raw.lower())
            try:
                quantity = int(quantity_str)
                if service and 0 < quantity <= 200:
                    for i in range(quantity):
                        # Создаем уникальный, но понятный номер для каждой записи в пакете
                        placeholder_number = f"{service.upper()}_BATCH_{now.strftime('%H%M%S%f')}_{i+1}" 
                        records_to_insert.append((placeholder_number, service, user.id, user.username, user.full_name, now_localized_str, message.chat.id))
                    accepted_summary[service] += quantity
            except (ValueError, TypeError):
                continue
        # Удаляем обработанные пакетные записи из текста, чтобы они не мешали поиску одиночных номеров
        text_for_numbers = BATCH_QUANTITY_PATTERN.sub('', text_to_process)

    # Поиск и обработка одиночных номеров
    patterns = {
        "Яндекс": YANDEX_SCOOTER_PATTERN,
        "Whoosh": WOOSH_SCOOTER_PATTERN,
        "Jet": JET_SCOOTER_PATTERN
    }
    
    processed_numbers = set() # Множество для отслеживания уже обработанных номеров

    for service, pattern in patterns.items():
        numbers = pattern.findall(text_for_numbers)
        for num in numbers:
            clean_num = num.replace('-', '') if service == "Jet" else num.upper()
            
            if clean_num in processed_numbers: # Проверяем, не был ли номер уже добавлен
                continue
            
            records_to_insert.append((clean_num, service, user.id, user.username, user.full_name, now_localized_str, message.chat.id))
            accepted_summary[service] += 1
            processed_numbers.add(clean_num) # Добавляем номер в список обработанных

    if not records_to_insert:
        return False # Ничего не найдено и не обработано

    await db_write_batch(records_to_insert)

    response_parts = []
    user_mention = types.User.get_mention(user)
    total_accepted = sum(accepted_summary.values())
    response_parts.append(f"{user_mention}, принято {total_accepted} шт.:")

    for service, count in sorted(accepted_summary.items()):
        if count > 0:
            response_parts.append(f"  - <b>{service}</b>: {count} шт.")

    await message.reply("\n".join(response_parts))
    return True # Что-то было найдено и обработано


# --- Обработчики сообщений ---
# Обработчик обычных текстовых сообщений
@dp.message_handler(IsAllowedChatFilter(), content_types=types.ContentTypes.TEXT)
async def handle_text_messages(message: types.Message):
    if message.text.startswith('/'): # Пропускаем команды, они обрабатываются другими хэндлерами
        return
    await process_scooter_text(message, message.text)

# Обработчик фотографий (с подписью)
@dp.message_handler(IsAllowedChatFilter(), content_types=types.ContentTypes.PHOTO)
async def handle_photo_messages(message: types.Message):
    if message.caption: # Если у фото есть подпись, пытаемся её обработать
        await process_scooter_text(message, message.caption)
    # else: Если подписи нет, бот ничего не будет отвечать, просто проигнорирует.

# Обработчик для всех остальных типов контента (видео, аудио, документы и т.д.)
# Этот обработчик должен быть ПОСЛЕДНИМ, чтобы не перехватывать другие типы сообщений.
@dp.message_handler(IsAllowedChatFilter(), content_types=types.ContentTypes.ANY)
async def handle_unsupported_content(message: types.Message):
    if message.text and message.text.startswith('/'): # Игнорируем команды
        return
    # Если это не фото и не обычный текст, сообщаем пользователю, что не поддерживаем
    if not (message.photo or (message.text and not message.text.startswith('/'))):
        await message.reply("Извините, я могу обрабатывать только текстовые сообщения и фотографии (с подписями). "
                            "Видео, документы и другие файлы я не поддерживаю.")

# --- Функции, выполняемые при запуске и остановке бота ---
async def on_startup(dispatcher: Dispatcher):
    global db_executor # Объявляем, что используем глобальную переменную
    db_executor = ThreadPoolExecutor(max_workers=5) # Инициализируем пул потоков здесь
    
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(db_executor, init_db) # Инициализируем БД в отдельном потоке
    
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
    global db_executor # Объявляем, что используем глобальную переменную
    if db_executor:
        db_executor.shutdown(wait=True) # Корректное завершение пула потоков
    logging.info("Пул потоков БД остановлен.")
    logging.info("Бот остановлен.")

# Запуск бота
if __name__ == "__main__":
    executor.start_polling(dp, on_startup=on_startup, on_shutdown=on_shutdown, skip_updates=True)
