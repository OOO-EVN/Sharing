import asyncio
import re
import os # Импортируем модуль os для работы с переменными окружения
from aiogram import Bot, Dispatcher, types
from aiogram.filters import CommandStart
from aiogram.enums import ParseMode
from dotenv import load_dotenv # Импортируем функцию для загрузки .env

# Загружаем переменные окружения из .env файла
load_dotenv()

# --- КОНСТАНТЫ ---
# Получаем токен бота из переменной окружения BOT_TOKEN
BOT_TOKEN = os.getenv('BOT_TOKEN')
if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN не найден в .env файле. Пожалуйста, добавьте его.")

# Получаем ID администраторов из переменной окружения ADMIN_IDS
# Разделяем строку по запятым и преобразуем каждый ID в целое число
ADMIN_IDS = [int(admin_id) for admin_id in os.getenv('ADMIN_IDS', '').split(',') if admin_id.strip()]
if not ADMIN_IDS:
    print("Внимание: ADMIN_IDS не заданы в .env файле. Функции администратора будут недоступны.")


# Регулярные выражения для определения сервиса:
# Яндекс: 8 цифр (e.g., 00714326)
YANDEX_SCOOTER_PATTERN = re.compile(r'\b\d{8}\b')
# Whoosh: 2 заглавные буквы, 4 цифры (e.g., AD9568)
WOOSH_SCOOTER_PATTERN = re.compile(r'\b[A-Z]{2}\d{4}\b')
# Jet: 3 цифры, дефис, 3 цифры (e.g., 290-423)
JET_SCOOTER_PATTERN = re.compile(r'\b\d{3}-\d{3}\b')

# --- ИНИЦИАЛИЗАЦИЯ ---
bot = Bot(token=BOT_TOKEN, parse_mode=ParseMode.HTML)
dp = Dispatcher()

# --- ОБРАБОТЧИКИ СООБЩЕНИЙ ---

@dp.message(CommandStart())
async def command_start_handler(message: types.Message) -> None:
    """
    Обрабатывает команду /start.
    """
    await message.answer(f"Привет, {message.from_user.full_name}! Я готов принимать самокаты.")

# --- НОВАЯ ФУНКЦИЯ: ПРОВЕРКА АДМИНА ---
def is_admin(user_id: int) -> bool:
    """
    Проверяет, является ли пользователь администратором.
    """
    return user_id in ADMIN_IDS

@dp.message(lambda message: is_admin(message.from_user.id), CommandStart(magic=re.compile(r"admin.*")))
async def admin_start_handler(message: types.Message) -> None:
    """
    Обрабатывает команду /start admin для администраторов.
    Здесь можно добавить меню администратора.
    """
    await message.answer("Привет, Администратор! Что вы хотите сделать?")
    # TODO: Добавить здесь кнопки или команды для администратора,
    # например, для просмотра статистики или экспорта отчетов.


@dp.message()
async def handle_all_messages(message: types.Message) -> None:
    """
    Обрабатывает сообщения, определяет тип самоката и подсчитывает количество,
    затем отвечает с указанием сервиса и количества, упоминая отправителя.
    """
    text_to_check = ""

    # Объединяем текст сообщения и подпись к фото, если она есть
    if message.text:
        text_to_check += message.text
    if message.caption:
        text_to_check += " " + message.caption

    # Если есть текст для анализа
    if text_to_check:
        response_parts = []
        
        # 1. Проверяем номера Яндекс и считаем их количество
        yandex_numbers = YANDEX_SCOOTER_PATTERN.findall(text_to_check)
        yandex_count = len(yandex_numbers)
        
        if yandex_count > 0:
            response_parts.append(f"Принял Яндекс: {yandex_count}")

        # 2. Проверяем номера Whoosh и считаем их количество
        woosh_numbers = WOOSH_SCOOTER_PATTERN.findall(text_to_check)
        woosh_count = len(woosh_numbers)
        
        if woosh_count > 0:
            response_parts.append(f"Принял Whoosh: {woosh_count}")

        # 3. Проверяем номера Jet и считаем их количество
        jet_numbers = JET_SCOOTER_PATTERN.findall(text_to_check)
        jet_count = len(jet_numbers)
        
        if jet_count > 0:
            response_parts.append(f"Принял Jet: {jet_count}")

        # Если найдены какие-либо номера (любого сервиса)
        if response_parts:
            # Получаем информацию об отправителе
            user_mention = message.from_user.full_name
            if message.from_user.username:
                user_mention = f"@{message.from_user.username}"
            else:
                # Если нет username, используем ссылку на профиль по ID
                user_mention = f"<a href='tg://user?id={message.from_user.id}'>{message.from_user.full_name}</a>"


            # Формируем окончательный ответ, добавляя упоминание
            final_response = f"{user_mention}, " + "\n".join(response_parts)
            
            # Отправляем ответ в чат
            await message.reply(final_response)

# --- ЗАПУСК БОТА ---
async def main() -> None:
    print("Бот запускается...")
    # Запускаем поллинг, чтобы бот начал принимать обновления
    await dp.start_polling(bot)
    print("Бот остановлен.")

if __name__ == "__main__":
    asyncio.run(main()
