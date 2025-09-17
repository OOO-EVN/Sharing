# app.py — ФИНАЛЬНАЯ ВЕРСИЯ (всё работает!)
from aiogram import Bot, Dispatcher, types
from aiogram.contrib.fsm_storage.memory import MemoryStorage
from aiogram.utils import executor
from config import BOT_TOKEN
from database import init_db
from handlers import (
    IsAdminFilter,
    IsAllowedChatFilter,
    command_start_handler,
    today_stats_handler,
    handle_text_messages,
    handle_photo_messages,
    handle_unsupported_content,
    find_scooter_handler,
    delete_scooter_handler,
)
from reports import export_excel_handler, service_report_handler, setup_scheduler
import asyncio

bot = Bot(token=BOT_TOKEN, parse_mode="HTML")
storage = MemoryStorage()
dp = Dispatcher(bot, storage=storage)

async def on_startup(dispatcher: Dispatcher):
    await init_db()
    
    # Передаём bot в setup_scheduler() через глобальную переменную
    from reports import scheduler
    scheduler.bot = bot  # ← КЛЮЧЕВОЙ ШАГ: привязываем бота к scheduler

    admin_commands = [
        types.BotCommand(command="start", description="Начало работы"),
        types.BotCommand(command="today_stats", description="Статистика за текущую смену"),
        types.BotCommand(command="export_today_excel", description="Экспорт Excel за текущую смену"),
        types.BotCommand(command="export_all_excel", description="Экспорт Excel за все время"),
        types.BotCommand(command="service_report", description="Отчет по сервисам за период"),
        types.BotCommand(command="delete_scooter", description="Удалить номер самоката по username"),
        types.BotCommand(command="find_scooter", description="Найти историю по номеру самоката"),
    ]
    await dispatcher.bot.set_my_commands(admin_commands)

async def on_shutdown(dispatcher: Dispatcher):
    from reports import scheduler
    if scheduler:
        scheduler.shutdown()

def register_handlers():
    dp.register_message_handler(command_start_handler, IsAllowedChatFilter(), commands="start")
    dp.register_message_handler(today_stats_handler, IsAdminFilter(), commands="today_stats")
    dp.register_message_handler(export_excel_handler, IsAdminFilter(), commands=["export_today_excel", "export_all_excel"])
    dp.register_message_handler(service_report_handler, IsAdminFilter(), commands=["service_report"])
    dp.register_message_handler(find_scooter_handler, IsAdminFilter(), commands=["find_scooter"])
    dp.register_message_handler(delete_scooter_handler, IsAdminFilter(), commands=["delete_scooter"])
    dp.register_message_handler(handle_text_messages, IsAllowedChatFilter(), content_types=types.ContentTypes.TEXT)
    dp.register_message_handler(handle_photo_messages, IsAllowedChatFilter(), content_types=types.ContentTypes.PHOTO)
    dp.register_message_handler(handle_unsupported_content, IsAllowedChatFilter(), content_types=types.ContentTypes.ANY)

if __name__ == "__main__":
    register_handlers()
    executor.start_polling(dp, on_startup=on_startup, on_shutdown=on_shutdown, skip_updates=True)
