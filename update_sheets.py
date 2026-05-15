#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Автоматический парсер курсов валют с banki.ru
GitHub Actions работает в UTC (Москва = UTC+3)
"""

import os
import re
import time
import requests
from lxml import html
import gspread
from google.oauth2.service_account import Credentials
import logging
import json
import traceback
from datetime import datetime
import pytz

# Настройка времени в логах (московское время)
moscow_tz = pytz.timezone('Europe/Moscow')
logging.Formatter.converter = lambda *args: datetime.now(moscow_tz).timetuple()
logging.basicConfig(
    level=logging.INFO, 
    format='%(asctime)s MSK - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)

# Читаем переменные окружения
SPREADSHEET_ID = os.environ.get("SPREADSHEET_ID", "").strip()
CURRENCIES_TO_PARSE = os.environ.get("CURRENCIES", "USD,EUR,GBP,CHF,JPY,CNY,AED,TRY,THB,EGP,KZT,BYN").split(",")

def init_google_sheets():
    logger.info("🔑 Начинаю инициализацию Google Sheets...")
    try:
        creds_json = os.environ.get("GOOGLE_CREDENTIALS")
        if not creds_json:
            raise ValueError("GOOGLE_CREDENTIALS не установлена")
        logger.info("📄 GOOGLE_CREDENTIALS найдена")
        
        creds_data = json.loads(creds_json)
        logger.info("✅ JSON ключей распарсен")
        
        creds = Credentials.from_service_account_info(
            creds_data, 
            scopes=["https://www.googleapis.com/auth/spreadsheets"]
        )
        client = gspread.authorize(creds)
        logger.info("✅ Авторизация в Google Sheets успешна")
        return client
    except json.JSONDecodeError as e:
        logger.error(f"❌ Ошибка парсинга JSON ключей: {e}")
        raise
    except Exception as e:
        logger.error(f"❌ Ошибка авторизации: {type(e).__name__}: {str(e)}")
        raise

def get_or_create_sheet(workbook, currency_code):
    currency_upper = currency_code.upper().strip()
    try:
        sheet = workbook.worksheet(currency_upper)
        logger.info(f"📄 Лист '{currency_upper}' найден")
    except gspread.exceptions.WorksheetNotFound:
        logger.info(f"📄 Лист '{currency_upper}' не найден, создаю...")
        sheet = workbook.add_worksheet(title=currency_upper, rows=200, cols=20)
        headers = ["Название банка", f"Покупка {currency_upper}", f"Продажа {currency_upper}", "Время обновления"]
        sheet.update('A2:D2', [headers])
        sheet.format('A2:D2', {'textFormat': {'bold': True}})
        logger.info(f"✅ Лист '{currency_upper}' создан")
    return sheet


#==================================================================
def parse_banki_ru(currency_code):
    """Парсинг текущих курсов валют с banki.ru"""
    currency = currency_code.lower().strip()
    currency_upper = currency_code.upper()
    url = f"https://www.banki.ru/products/currency/cash/{currency}/moskva/"
    
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    }
    
    try:
        logger.info(f"🌐 Запрос к {url}")
        response = requests.get(url, headers=headers, timeout=15)
        response.raise_for_status()
        tree = html.fromstring(response.content)
        
        # Ищем ТОЛЬКО таблицу с банками (не историю!)
        # Banki.ru использует specific классы для таблицы курсов
        table = tree.xpath("//table[contains(@class, 'js-currency-table') or contains(@class, 'cash-table')]")
        
        if not table:
            # Пробуем альтернативный селектор - ищем таблицу с заголовками "Покупка" и "Продажа"
            all_tables = tree.xpath("//table")
            for tbl in all_tables:
                headers_text = ' '.join(tbl.xpath('.//th//text()')).lower()
                if 'покупка' in headers_text and 'продажа' in headers_text:
                    table = [tbl]
                    logger.info("✅ Найдена таблица по заголовкам 'Покупка/Продажа'")
                    break
        
        if not table:
            logger.error(f"❌ Не найдена таблица с курсами банков")
            return []
        
        # Берем все строки с данными (пропускаем заголовки)
        rows = table[0].xpath(".//tr[td]")
        
        parsed_data = []
        
        for row in rows:
            cells = row.xpath(".//td | .//th")
            if len(cells) < 3:
                continue
            
            # Извлекаем текст из ячеек
            cell_values = []
            for cell in cells:
                # Ищем название банка (обычно в ссылке)
                bank_link = cell.xpath(".//a[contains(@class, 'bank-name')]//text()")
                if bank_link:
                    cell_values.append(bank_link[0].strip())
                else:
                    # Берем весь текст из ячейки
                    text = ' '.join(cell.xpath(".//text()"))
                    text = re.sub(r'\s+', ' ', text.strip())
                    # Убираем лишние символы
                    text = re.sub(r'[•\-\*]\s*', '', text)
                    if text:
                        cell_values.append(text)
            
            # Фильтруем: должно быть минимум 3 значения
            cell_values = [v for v in cell_values if v and len(v) > 1]
            
            if len(cell_values) >= 3:
                bank_name = cell_values[0]
                
                # Пропускаем, если это не банк (дата, заголовок и т.д.)
                if re.search(r'\d{4}\s*г\.?', bank_name) or 'мая|июня|июля|августа' in bank_name.lower():
                    continue
                
                # Извлекаем курсы (покупка и продажа)
                buy = cell_values[1].replace(',', '.')
                sell = cell_values[2].replace(',', '.')
                
                # Проверяем, что это числа
                try:
                    float(buy)
                    float(sell)
                    parsed_data.append([bank_name, buy, sell])
                except ValueError:
                    continue
        
        logger.info(f"📊 Для {currency_upper} извлечено {len(parsed_data)} банков")
        return parsed_data
        
    except Exception as e:
        logger.error(f"🔍 Ошибка парсинга {currency_upper}: {e}")
        return []
#==================================================================

def update_sheet_data(sheet, data, currency_code):
    try:
        if not data:
            logger.warning(f"⚠️ Нет данных для {currency_code}")
            return False
        
        last_row = sheet.row_count
        if last_row > 2:
            sheet.batch_clear([f"A3:D{last_row}"])
        
        rows_to_write = []
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
        
        for row in data:
            # Заменяем точки на запятые в числовых значениях (столбцы B и C)
            row_with_time = []
            for idx, value in enumerate(row):
                if idx in [1, 2]:  # Столбцы с курсами (индексы 1 и 2)
                    # Заменяем точку на запятую
                    value = value.replace('.', ',')
                row_with_time.append(value)
            
            row_with_time.append(timestamp)
            rows_to_write.append(row_with_time)
        
        sheet.update(values=rows_to_write, range_name=f"A3:D{len(rows_to_write) + 2}")
        logger.info(f"✅ Лист {currency_code} обновлён: {len(rows_to_write)} записей")
        return True
        
    except Exception as e:
        logger.error(f"❌ Ошибка записи в {currency_code}: {e}")
        return False

#==================================================================

def main():
    logger.info("=" * 70)
    logger.info("🚀 ЗАПУСК ПАРСЕРА КУРСОВ ВАЛЮТ")
    logger.info("=" * 70)
    
    # Проверяем переменные окружения
    logger.info("📋 ПРОВЕРКА ПЕРЕМЕННЫХ ОКРУЖЕНИЯ:")
    logger.info(f"   SPREADSHEET_ID длина: {len(SPREADSHEET_ID)} символов")
    logger.info(f"   SPREADSHEET_ID пустой? {not SPREADSHEET_ID}")
    logger.info(f"   CURRENCIES: {', '.join(CURRENCIES_TO_PARSE)}")
    logger.info("=" * 70)
    
    if not SPREADSHEET_ID:
        logger.error("❌ ОШИБКА: SPREADSHEET_ID не установлен!")
        logger.error("   Проверь Settings → Secrets and variables → Actions")
        return
    
    if len(SPREADSHEET_ID) != 44:
        logger.warning(f"⚠️ ВНИМАНИЕ: Длина SPREADSHEET_ID = {len(SPREADSHEET_ID)} (ожидается 44)")
    
    # Пробуем подключиться к Google Sheets
    try:
        logger.info("🔄 Инициализация Google Sheets...")
        client = init_google_sheets()
        
        logger.info(f"🔄 Открываю таблицу по ID...")
        workbook = client.open_by_key(SPREADSHEET_ID)
        logger.info(f"✅ ТАБЛИЦА ОТКРЫТА УСПЕШНО!")
        logger.info(f"   Название таблицы: {workbook.title}")
        
    except gspread.exceptions.SpreadsheetNotFound:
        logger.error("❌ ОШИБКА: Таблица не найдена (SpreadsheetNotFound)")
        logger.error(f"   Возможные причины:")
        logger.error(f"   1. Неверный ID таблицы")
        logger.error(f"   2. Сервисный аккаунт НЕ добавлен в настройки доступа таблицы")
        logger.error(f"   3. Google Sheets API не включён в Google Cloud Console")
        return
    except gspread.exceptions.APIError as e:
        logger.error(f"❌ ОШИБКА API Google Sheets: {e}")
        logger.error(f"   Проверь права доступа сервисного аккаунта")
        return
    

    except Exception as e:
        logger.error(f"❌ Неожиданная ошибка: {type(e).__name__}: {str(e)}")
        logger.error(f"Traceback:\n{traceback.format_exc()}")
        return
    
    # Парсим валюты
    success_count = 0
    for currency in CURRENCIES_TO_PARSE:
        currency = currency.strip().upper()
        if not currency:
            continue
            
        logger.info(f"\n{'='*50}")
        logger.info(f"💱 Обработка валюты: {currency}")
        logger.info(f"{'='*50}")
        
        try:
            sheet = get_or_create_sheet(workbook, currency)
            data = parse_banki_ru(currency)
            
            if data and update_sheet_data(sheet, data, currency):
                success_count += 1
            
            time.sleep(2)  # Пауза между запросами
            
        except Exception as e:
            logger.error(f"❌ Ошибка для {currency}: {e}")
            continue
    
    # Итоги
    total = len([c for c in CURRENCIES_TO_PARSE if c.strip()])
    logger.info(f"\n{'='*70}")
    logger.info(f"🏁 ЗАВЕРШЕНИЕ РАБОТЫ")
    logger.info(f"   Успешно обновлено: {success_count} из {total} валют")
    logger.info(f"{'='*70}")

if __name__ == "__main__":
    main()
