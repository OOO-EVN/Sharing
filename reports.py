from aiogram import types
from config import TIMEZONE, REPORT_CHAT_IDS, bot
from database import db_fetch_all
from openpyxl import Workbook
from openpyxl.styles import Font, Alignment
from openpyxl.utils import get_column_letter
from io import BytesIO
import datetime
import logging
import re
from collections import defaultdict
from apscheduler.schedulers.asyncio import AsyncIOScheduler

scheduler = None

def create_excel_report(records: list[tuple]) -> BytesIO:
    wb = Workbook()
    ws_all_data = wb.active
    ws_all_data.title = "Все данные"

    headers_all_data = ["ID", "Номер Самоката", "Сервис", "ID Пользователя", "Ник", "Полное имя", "Время Принятия", "ID Чата"]
    ws_all_data.append(headers_all_data)
    header_font = Font(bold=True)
    for cell in ws_all_data[1]:
        cell.font = header_font

    for row in records:
        ws_all_data.append(row)

    for col_idx, col in enumerate(ws_all_data.columns):
        max_length = 0
        column_letter = get_column_letter(col_idx + 1)
        for cell in col:
            try:
                if cell.value:
                    max_length = max(max_length, len(str(cell.value)))
            except:
                pass
        adjusted_width = (max_length + 2) * 1.2
        ws_all_data.column_dimensions[column_letter].width = adjusted_width

    ws_totals = wb.create_sheet("Итоги")
    totals_headers = ["Пользователь", "Всего Самокатов"]
    ws_totals.append(totals_headers)
    for cell in ws_totals[1]:
        cell.font = header_font

    user_total_counts_summary = defaultdict(int)
    user_info_map_summary = {}

    for record in records:
        user_id = record[3]
        username = record[4]
        fullname = record[5]
        display_name = fullname if fullname else (f"@{username}" if username else f"ID: {user_id}")
        user_total_counts_summary[user_id] += 1
        user_info_map_summary[user_id] = display_name

    sorted_user_ids_summary = sorted(user_total_counts_summary.keys(), key=lambda user_id: user_info_map_summary[user_id].lower())

    for user_id in sorted_user_ids_summary:
        user_display_name = user_info_map_summary[user_id]
        total_count = user_total_counts_summary[user_id]
        ws_totals.append([user_display_name, total_count])
        ws_totals.cell(row=ws_totals.max_row, column=1).font = Font(bold=True)
        ws_totals.cell(row=ws_totals.max_row, column=2).font = Font(bold=True)

    for col_idx, col in enumerate(ws_totals.columns):
        max_length = 0
        column_letter = get_column_letter(col_idx + 1)
        for cell in col:
            try:
                if cell.value:
                    max_length = max(max_length, len(str(cell.value)))
            except:
                pass
        adjusted_width = (max_length + 2) * 1.2
        ws_totals.column_dimensions[column_letter].width = adjusted_width

    user_records = defaultdict(list)
    user_info_for_sheets = {}

    for record in records:
        user_id = record[3]
        username = record[4]
        fullname = record[5]
        display_name = fullname if fullname else (f"@{username}" if username else f"ID: {user_id}")
        
        user_records[user_id].append(record)
        if user_id not in user_info_for_sheets:
            user_info_for_sheets[user_id] = display_name

    sorted_user_ids = sorted(user_records.keys(), key=lambda user_id: user_info_for_sheets[user_id].lower())

    user_sheet_headers = ["ID", "Номер Самоката", "Сервис", "Время Принятия", "ID Чата"]

    for user_id in sorted_user_ids:
        user_display_name = user_info_for_sheets[user_id]
        sheet_name_raw = f"{user_display_name[:25].replace('@', '')}"
        invalid_chars = re.compile(r'[\\/:*?"<>|]')
        sheet_name = invalid_chars.sub('', sheet_name_raw)
        
        if not sheet_name or len(sheet_name) < 3:
             sheet_name = f"ID{user_id}"
        
        original_sheet_name = sheet_name
        counter = 1
        while sheet_name in wb.sheetnames:
            sheet_name = f"{original_sheet_name[:28]}{counter}"
            counter += 1

        ws_user = wb.create_sheet(title=sheet_name)
        ws_user.append(user_sheet_headers)
        for cell in ws_user[1]:
            cell.font = header_font

        current_user_total = 0
        user_service_breakdown = defaultdict(int)

        for record in user_records[user_id]:
            row_to_add = [record[0], record[1], record[2], record[6], record[7]]
            ws_user.append(row_to_add)
            current_user_total += 1
            user_service_breakdown[record[2]] += 1

        ws_user.append([])
        ws_user.append(["Статистика по сервисам:"])
        ws_user.cell(row=ws_user.max_row, column=1).font = Font(bold=True)
        ws_user.merge_cells(start_row=ws_user.max_row, start_column=1, end_row=ws_user.max_row, end_column=2)

        for service, count in sorted(user_service_breakdown.items()):
            ws_user.append([service, count])

        ws_user.append([])
        ws_user.append(["Всего принято:", current_user_total])
        ws_user.cell(row=ws_user.max_row, column=1).font = Font(bold=True)
        ws_user.cell(row=ws_user.max_row, column=2).font = Font(bold=True)
        ws_user.cell(row=ws_user.max_row, column=1).alignment = Alignment(horizontal='right')
        ws_user.cell(row=ws_user.max_row, column=2).alignment = Alignment(horizontal='center')

        for col_idx, col in enumerate(ws_user.columns):
            max_length = 0
            column_letter = get_column_letter(col_idx + 1)
            for cell in col:
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
        query += " ORDER BY timestamp DESC"
        records = await db_fetch_all(query)
        date_filter_text = " за все время"

    if not records:
        await message.answer(f"Нет данных для экспорта{date_filter_text}.")
        return

    try:
        excel_file = create_excel_report(records)
        report_type = "shift" if is_today_shift else "full"
        filename = f"report_{report_type}_{datetime.date.today().isoformat()}.xlsx"
        await bot.send_document(message.chat.id, types.InputFile(excel_file, filename=filename), caption=f"Ваш отчет{date_filter_text} готов.")
    except Exception as e:
        logging.error(f"Ошибка при отправке Excel файла: {e}")
        await message.answer("Произошла ошибка при отправке отчета.")

async def service_report_handler(message: types.Message):
    args = message.get_args().split()
    if len(args) != 2:
        await message.reply("Используйте: /service_report <начало> <конец>\nПример: /service_report 2024-07-15 2024-07-25")
        return

    start_date_str, end_date_str = args
    try:
        start_date = TIMEZONE.localize(datetime.datetime.strptime(start_date_str, "%Y-%m-%d"))
        end_date = TIMEZONE.localize(datetime.datetime.strptime(end_date_str, "%Y-%m-%d") + datetime.timedelta(days=1))
    except Exception as e:
        await message.reply("Некорректный формат даты. Дата должна быть в YYYY-MM-DD.")
        return

    report_lines = []
    total_all = 0
    total_service = defaultdict(int)

    current_date = start_date
    while current_date <= end_date:
        morning_start = TIMEZONE.localize(datetime.datetime.combine(current_date.date(), datetime.time(7, 0, 0)))
        morning_end = TIMEZONE.localize(datetime.datetime.combine(current_date.date(), datetime.time(15, 0, 0)))
        
        morning_query = "SELECT service FROM accepted_scooters WHERE timestamp BETWEEN ? AND ?"
        morning_records = await db_fetch_all(morning_query, (morning_start.strftime("%Y-%m-%d %H:%M:%S"), morning_end.strftime("%Y-%m-%d %H:%M:%S")))

        morning_services = defaultdict(int)
        for (service,) in morning_records:
            morning_services[service] += 1
            total_service[service] += 1
            total_all += 1

        evening_start = TIMEZONE.localize(datetime.datetime.combine(current_date.date(), datetime.time(15, 0, 0)))
        evening_end = TIMEZONE.localize(datetime.datetime.combine(current_date.date() + datetime.timedelta(days=1), datetime.time(4, 0, 0)))
        
        evening_query = "SELECT service FROM accepted_scooters WHERE timestamp BETWEEN ? AND ?"
        even_records = await db_fetch_all(evening_query, (evening_start.strftime("%Y-%m-%d %H:%M:%S"), evening_end.strftime("%Y-%m-%d %H:%M:%S")))

        even_services = defaultdict(int)
        for (service,) in even_records:
            even_services[service] += 1
            total_service[service] += 1
            total_all += 1

        date_str = current_date.strftime("%d.%m")
        report_lines.append(f"<b>{date_str}</b>")
        report_lines.append("Утренняя смена (7:00-15:00):")
        for service, count in sorted(morning_services.items()):
            report_lines.append(f"{service}: {count} шт.")
        report_lines.append("Вечерняя смена (15:00-4:00):")
        for service, count in sorted(even_services.items()):
            report_lines.append(f"{service}: {count} шт.")
        report_lines.append("")

        current_date += datetime.timedelta(days=1)

    report_lines.append("<b>Итог по сервисам за период:</b>")
    for service, count in sorted(total_service.items()):
        report_lines.append(f"{service}: {count} шт.")
    report_lines.append(f"\n<b>Общий итог: {total_all} шт.</b>")

    report_text = '\n'.join(report_lines)
    MESSAGE_LIMIT = 4000
    buffer = []
    for line in report_lines:
        if len('\n'.join(buffer + [line])) > MESSAGE_LIMIT:
            await message.answer('\n'.join(buffer))
            buffer = []
        buffer.append(line)
    
    if buffer:
        await message.answer('\n'.join(buffer))

async def send_scheduled_report(shift_type: str):
    start_time, end_time, shift_name = get_shift_time_range_for_report(shift_type)
    
    if not start_time or not end_time:
        return

    start_str = start_time.strftime("%Y-%m-%d %H:%M:%S")
    end_str = end_time.strftime("%Y-%m-%d %H:%M:%S")

    query = "SELECT id, scooter_number, service, accepted_by_user_id, accepted_by_username, accepted_by_fullname, timestamp, chat_id FROM accepted_scooters WHERE timestamp BETWEEN ? AND ?"
    records = await db_fetch_all(query, (start_str, end_str))

    if not records:
        message_text = f"Отчет за {shift_name} ({start_time.strftime('%H:%M')} - {end_time.strftime('%H:%M')}): За смену ничего не принято."
        for chat_id in REPORT_CHAT_IDS:
            try:
                await bot.send_message(chat_id, message_text)
            except Exception:
                pass
        return

    try:
        excel_file = create_excel_report(records)
        report_type_filename = "morning_shift" if shift_type == 'morning' else "evening_shift"
        filename = f"report_{report_type_filename}_{start_time.strftime('%Y%m%d')}.xlsx"
        caption = f"Ежедневный отчет за {shift_name} ({start_time.strftime('%d.%m %H:%M')} - {end_time.strftime('%d.%m %H:%M')})"
        
        for chat_id in REPORT_CHAT_IDS:
            try:
                excel_file.seek(0)
                await bot.send_document(chat_id, types.InputFile(excel_file, filename=filename), caption=caption)
            except Exception:
                pass
    except Exception:
        pass

def get_shift_time_range_for_report(shift_type: str):
    now = datetime.datetime.now(TIMEZONE)
    today = now.date()
    
    if shift_type == 'morning':
        start_time = TIMEZONE.localize(datetime.datetime.combine(today, datetime.time(7, 0, 0)))
        end_time = TIMEZONE.localize(datetime.datetime.combine(today, datetime.time(15, 0, 0)))
        shift_name = "утреннюю смену"
    elif shift_type == 'evening':
        evening_start_actual = TIMEZONE.localize(datetime.datetime.combine(today, datetime.time(15, 0, 0)))
        evening_end_extended = TIMEZONE.localize(datetime.datetime.combine(today + datetime.timedelta(days=1), datetime.time(4, 0, 0)))
        start_time = evening_start_actual
        end_time = evening_end_extended
        shift_name = "вечернюю смену (с учетом ночных часов)"
    else:
        return None, None, None
        
    return start_time, end_time, shift_name

def setup_scheduler():
    global scheduler
    scheduler = AsyncIOScheduler(timezone=str(TIMEZONE))
    scheduler.add_job(send_scheduled_report, 'cron', hour=15, minute=0, timezone=str(TIMEZONE), args=['morning'])
    scheduler.add_job(send_scheduled_report, 'cron', hour=23, minute=0, timezone=str(TIMEZONE), args=['evening'])
    scheduler.start()