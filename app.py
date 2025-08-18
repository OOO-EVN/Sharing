from aiogram import Bot, Dispatcher, types
from aiogram.contrib.fsm_storage.memory import MemoryStorage
from aiogram.utils import executor
from config import BOT_TOKEN
from database import init_db, db_executor
from handlers import (
    IsAdminFilter,
    IsAllowedChatFilter,
    command_start_handler,
    today_stats_handler,
    handle_text_messages,
    handle_photo_messages,
    handle_unsupported_content,
)
from reports import export_excel_handler, service_report_handler, setup_scheduler
from concurrent.futures import ThreadPoolExecutor
import asyncio

bot = Bot(token=BOT_TOKEN, parse_mode="HTML")
storage = MemoryStorage()
dp = Dispatcher(bot, storage=storage)

async def on_startup(dispatcher: Dispatcher):
    global db_executor
    db_executor = ThreadPoolExecutor(max_workers=5)

    loop = asyncio.get_running_loop()
    await loop.run_in_executor(db_executor, init_db)

    setup_scheduler()

    admin_commands = [
        types.BotCommand(command="start", description="Начало работы"),
        types.BotCommand(command="today_stats", description="Статистика за текущую смену"),
        types.BotCommand(command="export_today_excel", description="Экспорт Excel за текущую смену"),
        types.BotCommand(command="export_all_excel", description="Экспорт Excel за все время"),
        types.BotCommand(command="service_report", description="Отчет по сервисам за период"),
    ]
    await dispatcher.bot.set_my_commands(admin_commands)

async def on_shutdown(dispatcher: Dispatcher):
    global db_executor
    if db_executor:
        db_executor.shutdown(wait=True)

    from reports import scheduler
    if scheduler:
        scheduler.shutdown()

def register_handlers():
    dp.register_message_handler(command_start_handler, IsAllowedChatFilter(), commands="start")
    dp.register_message_handler(today_stats_handler, IsAdminFilter(), commands="today_stats")
    dp.register_message_handler(export_excel_handler, IsAdminFilter(), commands=["export_today_excel", "export_all_excel"])
    dp.register_message_handler(service_report_handler, IsAdminFilter(), commands=["service_report"])
    dp.register_message_handler(handle_text_messages, IsAllowedChatFilter(), content_types=types.ContentTypes.TEXT)
    dp.register_message_handler(handle_photo_messages, IsAllowedChatFilter(), content_types=types.ContentTypes.PHOTO)
    dp.register_message_handler(handle_unsupported_content, IsAllowedChatFilter(), content_types=types.ContentTypes.ANY)

if __name__ == "__main__":
    register_handlers()
    executor.start_polling(dp, on_startup=on_startup, on_shutdown=on_shutdown, skip_updates=True)
