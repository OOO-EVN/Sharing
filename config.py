# config.py
import os
import re
import logging
from dotenv import load_dotenv
import pytz
from aiogram import Bot  # Add aiogram import

# Logging configuration
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('bot.log'),
        logging.StreamHandler()
    ]
)

# Load environment variables
load_dotenv()

# Bot token
BOT_TOKEN = os.getenv('BOT_TOKEN')
if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN не найден в .env файле.")

# Initialize bot
bot = Bot(token=BOT_TOKEN)  # Initialize aiogram Bot

# Admin and chat IDs
try:
    ADMIN_IDS = {int(admin_id) for admin_id in os.getenv('ADMIN_IDS', '').split(',') if admin_id.strip()}
    ALLOWED_CHAT_IDS = {int(chat_id) for chat_id in os.getenv('ALLOWED_CHAT_IDS', '').split(',') if chat_id.strip()}
    REPORT_CHAT_IDS = {int(chat_id) for chat_id in os.getenv('REPORT_CHAT_IDS', '').split(',') if chat_id.strip()}
except ValueError:
    logging.error("Не удалось прочитать ADMIN_IDS, ALLOWED_CHAT_IDS или REPORT_CHAT_IDS.")
    ADMIN_IDS = set()
    ALLOWED_CHAT_IDS = set()
    REPORT_CHAT_IDS = set()

# Database and timezone
DB_NAME = 'scooters.db'
TIMEZONE = pytz.timezone('Asia/Almaty')

# Regular expression patterns for scooter numbers
YANDEX_SCOOTER_PATTERN = re.compile(r'\b(\d{8})\b')
WOOSH_SCOOTER_PATTERN = re.compile(r'\b([A-ZА-Я]{2}\d{4})\b', re.IGNORECASE)
JET_SCOOTER_PATTERN = re.compile(r'\b(\d{3}-?\d{3})\b')

# Pattern for batch quantity
BATCH_QUANTITY_PATTERN = re.compile(r'\b(whoosh|jet|yandex|вуш|джет|яндекс|w|j|y)\s+(\d+)\b', re.IGNORECASE)

# Service aliases and mapping
SERVICE_ALIASES = {
    "yandex": "Яндекс", "яндекс": "Яндекс", "y": "Яндекс",
    "whoosh": "Whoosh", "вуш": "Whoosh", "w": "Whoosh",
    "jet": "Jet", "джет": "Jet", "j": "Jet"
}
SERVICE_MAP = {"yandex": "Яндекс", "whoosh": "Whoosh", "jet": "Jet"}
