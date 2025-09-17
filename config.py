# config.py — ОБЯЗАТЕЛЬНО ТАКОЙ!
import os
import re
import logging
from typing import Set
from dotenv import load_dotenv
import pytz

AdminIds = Set[int]
ChatIds = Set[int]

class Config:
    def __init__(self):
        load_dotenv()

        self.BOT_TOKEN = os.getenv('BOT_TOKEN')
        if not self.BOT_TOKEN:
            raise ValueError("BOT_TOKEN не найден в .env файле.")

        self.ADMIN_IDS = {int(x.strip()) for x in os.getenv('ADMIN_IDS', '').split(',') if x.strip()} or set()
        self.ALLOWED_CHAT_IDS = {int(x.strip()) for x in os.getenv('ALLOWED_CHAT_IDS', '').split(',') if x.strip()} or set()
        self.REPORT_CHAT_IDS = {int(x.strip()) for x in os.getenv('REPORT_CHAT_IDS', '').split(',') if x.strip()} or set()

        if not self.ADMIN_IDS:
            logging.warning("⚠️ ADMIN_IDS не заданы — команды администратора будут недоступны!")
        if not self.ALLOWED_CHAT_IDS:
            logging.warning("⚠️ ALLOWED_CHAT_IDS не заданы — бот не будет реагировать в группах!")

        self.DB_NAME = os.getenv('DB_NAME', 'scooters.db')
        timezone_name = os.getenv('TIMEZONE', 'Asia/Almaty')
        try:
            self.TIMEZONE = pytz.timezone(timezone_name)
        except pytz.exceptions.UnknownTimeZoneError:
            raise ValueError(f"Неизвестная таймзона: {timezone_name}")

        self.YANDEX_SCOOTER_PATTERN = re.compile(r'\b(\d{8})\b')
        self.WOOSH_SCOOTER_PATTERN = re.compile(r'\b([A-ZА-Я]{2}\d{4})\b', re.IGNORECASE)
        self.JET_SCOOTER_PATTERN = re.compile(r'\b(\d{6}|\d{3}-\d{3})\b')
        self.BOLT_SCOOTER_PATTERN = re.compile(r'\b(\d{4})\b')
        self.BATCH_QUANTITY_PATTERN = re.compile(r'\b(whoosh|jet|bolt|yandex|вуш|джет|болт|яндекс|w|j|b|y)\s+(\d+)\b', re.IGNORECASE)

        self.SERVICE_ALIASES = {
            "yandex": "Яндекс", "яндекс": "Яндекс", "y": "Яндекс",
            "whoosh": "Whoosh", "вуш": "Whoosh", "w": "Whoosh",
            "jet": "Jet", "джет": "Jet", "j": "Jet",
            "bolt": "Bolt", "болт": "Bolt", "b": "Bolt"
        }

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('bot.log'),
        logging.StreamHandler()
    ]
)

config = Config()
BOT_TOKEN = config.BOT_TOKEN
ADMIN_IDS = config.ADMIN_IDS
ALLOWED_CHAT_IDS = config.ALLOWED_CHAT_IDS
REPORT_CHAT_IDS = config.REPORT_CHAT_IDS
DB_NAME = config.DB_NAME
TIMEZONE = config.TIMEZONE
YANDEX_SCOOTER_PATTERN = config.YANDEX_SCOOTER_PATTERN
WOOSH_SCOOTER_PATTERN = config.WOOSH_SCOOTER_PATTERN
JET_SCOOTER_PATTERN = config.JET_SCOOTER_PATTERN
BOLT_SCOOTER_PATTERN = config.BOLT_SCOOTER_PATTERN
BATCH_QUANTITY_PATTERN = config.BATCH_QUANTITY_PATTERN
SERVICE_ALIASES = config.SERVICE_ALIASES
