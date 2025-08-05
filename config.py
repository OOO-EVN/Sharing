import os
import re
import logging
from dotenv import load_dotenv
import pytz
from aiogram import Bot

# Логгирование
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('bot.log'),
        logging.StreamHandler()
    ]
)
import os
import re
import logging
from dotenv import load_dotenv
import pytz
from aiogram import Bot

# Логгирование
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('bot.log'),
        logging.StreamHandler()
    ]
)

# Загрузка переменных окружения
load_dotenv()

# Токен бота
BOT_TOKEN = os.getenv('BOT_TOKEN')
if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN не найден в .env файле.")

# Инициализация бота
bot = Bot(token=BOT_TOKEN)

# ИД админов и чатов
try:
    ADMIN_IDS = {int(admin_id) for admin_id in os.getenv('ADMIN_IDS', '').split(',') if admin_id.strip()}
    ALLOWED_CHAT_IDS = {int(chat_id) for chat_id in os.getenv('ALLOWED_CHAT_IDS', '').split(',') if chat_id.strip()}
    REPORT_CHAT_IDS = {int(chat_id) for chat_id in os.getenv('REPORT_CHAT_IDS', '').split(',') if chat_id.strip()}
except ValueError:
    logging.error("Не удалось прочитать ADMIN_IDS, ALLOWED_CHAT_IDS или REPORT_CHAT_IDS.")
    ADMIN_IDS = set()
    ALLOWED_CHAT_IDS = set()
    REPORT_CHAT_IDS = set()

# Имя базы и таймзона
DB_NAME = 'scooters.db'
TIMEZONE = pytz.timezone('Asia/Almaty')

# Регулярки по номерам
YANDEX_SCOOTER_PATTERN = re.compile(r'\b(\d{8})\b')
WOOSH_SCOOTER_PATTERN = re.compile(r'\b([A-ZА-Я]{2}\d{4})\b', re.IGNORECASE)
JET_SCOOTER_PATTERN = re.compile(r'\b(\d{3}-?\d{3})\b')  # Используется и для Bolt

# Пакетный ввод — whoosh 3, bolt 5 и т.п.
BATCH_QUANTITY_PATTERN = re.compile(r'\b(whoosh|jet|bolt|yandex|вуш|джет|болт|яндекс|w|j|b|y)\s+(\d+)\b', re.IGNORECASE)
# Алиасы для всех сервисов
SERVICE_ALIASES = {
    "yandex": "Яндекс", "яндекс": "Яндекс", "y": "Яндекс",
    "whoosh": "Whoosh", "вуш": "Whoosh", "w": "Whoosh",
    "jet": "Jet", "джет": "Jet", "j": "Jet",
    "bolt": "Bolt", "болт": "Bolt", "b": "Bolt"
}

# Явная карта сервисов (если понадобится)
SERVICE_MAP = {
    "yandex": "Яндекс",
    "whoosh": "Whoosh",
    "jet": "Jet",
    "bolt": "Bolt"
}
