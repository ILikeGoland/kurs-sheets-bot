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

def parse_banki_ru(currency_code):
    currency = currency_code.lower().strip()
    currency_upper = currency_code.upper()
    url = f"https://www.banki.ru/products/currency/cash/{currency}/moskva/"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "ru-RU,ru;q=0.9"
    }
    
    try:
        logger.info(f"🌐 Запрос к {url}")
        response = requests.get(url, headers=headers, timeout=15)
        response.raise_for_status()
        tree = html.fromstring(response.content)
        tbody = tree.xpath("//table//tbody")
        
        if not tbody:
            logger.warning(f"⚠️ Таблица не найдена для {currency_upper}")
            return []
        
        rows = tbody[0].xpath("./tr")
        parsed_data = []
        
        for row in rows:
            cells = row.xpath("./th | ./td")
            if len(cells) < 4:
                continue
            
            row_values = []
            for idx, cell in enumerate(cells):
                texts = cell.xpath(".//text()")
                clean_texts = [t.strip() for t in texts if t.strip()]
                
                if not clean_texts:
                    row_values.append("")
                    continue
                    
                cell_text = " ".join(clean_texts)
                
                if idx == 0:
                    cell_text = re.sub(r'[•\-\*]\s*', '', cell_text)
                    cell_text = cell_text.split('\n')[0].strip()
                    if re.match(r'^\d+\s+[A-Z]{3}', cell_text):
                        continue
                
                row_values.append(cell_text)
            
            if row_values and row_values[0] and not row_values[0].startswith("1 "):
                parsed_data.append(row_values[:4])
        
        logger.info(f"📊 Для {currency_upper} извлечено {len(parsed_data)} записей")
        return parsed_data
        
    except Exception as e:
        logger.error(f"🔍 Ошибка парсинга для {currency_upper}: {e}")
        return []

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
            row_with_time = row + [timestamp] if len(row) == 3 else row[:3] + [timestamp]
            rows_to_write.append(row_with_time)
        
        sheet.update(f"A3:D{len(rows_to_write) + 2}", rows_to_write)
        logger.info(f"✅ Лист {currency_code} обновлён: {len(rows_to_write)} записей")
        return True
        
    except Exception as e:
        logger.error(f"❌ Ошибка записи в {currency_code}: {e}")
        return False

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
    except gspread.exceptions.AuthenticationError as e:
        logger.error(f"❌ ОШИБКА АУТЕНТИФИКАЦИИ: {e}")
        logger.error(f"   Проверь GOOGLE_CREDENTIALS в секретах")
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
