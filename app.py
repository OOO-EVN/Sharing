# app.py
from aiogram import Bot, Dispatcher, types
from aiogram.contrib.fsm_storage.memory import MemoryStorage
from aiogram.utils import executor
from config import BOT_TOKEN, REPORT_CHAT_IDS  # ← импортируем REPORT_CHAT_IDS здесь
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
    export_excel_handler,
    service_report_handler
)
import asyncio
import logging

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)

bot = Bot(token=BOT_TOKEN, parse_mode="HTML")
storage = MemoryStorage()
dp = Dispatcher(bot, storage=storage)

# Глобальный обработчик необработанных исключений
@dp.errors_handler()
async def errors_handler(update, exception):
    logging.exception(f"Update {update} вызвал ошибку: {exception}")
    return True

async def on_startup(dispatcher: Dispatcher):
    await init_db()
    
    from reports import scheduler, send_scheduled_report

    # Привязываем бота к scheduler (на случай, если понадобится внутри)
    scheduler.bot = bot

    # Добавляем запланированные задачи
    scheduler.add_job(
        send_scheduled_report,
        'cron',
        hour=15,
        minute=0,
        args=['morning', bot],
        id='morning_report',
        replace_existing=True
    )
    scheduler.add_job(
        send_scheduled_report,
        'cron',
        hour=23,
        minute=0,
        args=['evening', bot],
        id='evening_report',
        replace_existing=True
    )

    scheduler.start()

    logging.info(f"✅ Планировщик запущен. Задачи: {[job.id for job in scheduler.get_jobs()]}")
    logging.info(f"📤 REPORT_CHAT_IDS: {REPORT_CHAT_IDS}")

    
    #for chat_id in REPORT_CHAT_IDS:
        #try:
           # await bot.send_message(
            #    chat_id,
           #     "✅ Бот успешно запущен.\n"
          #      "Запланированные отчёты будут приходить в 15:00 и 23:00 по времени UTC+5."
         #   )
        #    logging.info(f"✅ Тестовое сообщение отправлено в чат {chat_id}")
       # except Exception as e:
           # logging.error(f"❌ НЕ УДАЛОСЬ отправить сообщение в чат {chat_id}: {e}")

    # Устанавливаем команды для админов
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
    logging.info("✅ Команды бота обновлены")

async def on_shutdown(dispatcher: Dispatcher):
    from reports import scheduler
    try:
        if hasattr(scheduler, 'running') and scheduler.running:
            scheduler.shutdown(wait=False)
            logging.info("⏹️ Планировщик остановлен")
    except Exception as e:
        logging.warning(f"⚠️ Ошибка при остановке планировщика: {e}")

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
