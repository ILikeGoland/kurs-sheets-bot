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
    """Парсинг курсов валют с banki.ru"""
    currency = currency_code.lower().strip()
    currency_upper = currency_code.upper()
    url = f"https://www.banki.ru/products/currency/cash/{currency}/moskva/"
    
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7"
    }
    
    try:
        logger.info(f"🌐 Запрос к {url}")
        response = requests.get(url, headers=headers, timeout=15)
        response.raise_for_status()
        
        # Сохраним HTML для отладки (можно посмотреть в логах)
        html_content = response.text
        tree = html.fromstring(html_content)
        
        # Ищем таблицу с курсами - пробуем разные селекторы
        # Banki.ru использует разные классы для таблиц
        rows = []
        
        # Пробуем найти по классу таблицы курсов
        table_selectors = [
            "//table[contains(@class, 'js-currency-table')]//tr[td]",
            "//table[@class='data-table']//tr[td]",
            "//div[contains(@class, 'currency-table')]//tr[td]",
            "//table//tr[contains(@class, 'item')]"
        ]
        
        for selector in table_selectors:
            rows = tree.xpath(selector)
            if rows:
                logger.info(f"✅ Найдено {len(rows)} строк по селектору: {selector[:50]}...")
                break
        
        if not rows:
            # Если не нашли таблицу, пробуем найти по data-атрибутам
            rows = tree.xpath("//tr[@data-id or @data-bank-id]")
            if not rows:
                logger.error(f"❌ Не удалось найти таблицу с курсами на странице")
                # Для отладки: ищем все таблицы
                all_tables = tree.xpath("//table")
                logger.info(f"📊 Найдено таблиц на странице: {len(all_tables)}")
                return []
        
        parsed_data = []
        
        for row in rows:
            # Извлекаем все ячейки
            cells = row.xpath(".//td | .//th")
            if len(cells) < 3:
                continue
            
            # Извлекаем текст из ячеек
            cell_texts = []
            for cell in cells:
                # Пробуем найти название банка
                bank_name = cell.xpath(".//a[@class='bank-name' or contains(@class, 'name')]//text()")
                if bank_name:
                    cell_texts.append(bank_name[0].strip())
                else:
                    # Если не нашли, берём весь текст
                    text = ' '.join(cell.xpath(".//text()"))
                    text = re.sub(r'\s+', ' ', text.strip())
                    if text:
                        cell_texts.append(text)
            
            # Фильтруем пустые строки
            cell_texts = [t for t in cell_texts if t and len(t) > 2]
            
            if len(cell_texts) >= 3:
                bank_name = cell_texts[0]
                
                # Проверяем, что это не заголовок и не дата
                if re.search(r'\d{4}', bank_name) or 'мая|июня|июля|августа|сентября|октября|ноября|декабря|января|февраля|марта|апреля' in bank_name.lower():
                    continue
                
                # Извлекаем курсы (покупаем/продаём)
                buy_rate = cell_texts[1] if len(cell_texts) > 1 else ""
                sell_rate = cell_texts[2] if len(cell_texts) > 2 else ""
                
                # Проверяем, что это числа
                if not re.search(r'\d+\.?\d*', buy_rate.replace(',', '.')) or not re.search(r'\d+\.?\d*', sell_rate.replace(',', '.')):
                    continue
                
                parsed_data.append([bank_name, buy_rate, sell_rate])
        
        logger.info(f"📊 Для {currency_upper} извлечено {len(parsed_data)} записей")
        
        if len(parsed_data) == 0:
            logger.warning(f"⚠️ Не удалось распарсить данные. Проверьте структуру сайта.")
        
        return parsed_data
        
    except Exception as e:
        logger.error(f"🔍 Ошибка парсинга для {currency_upper}: {e}")
        logger.error(f"Traceback:\n{traceback.format_exc()}")
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
