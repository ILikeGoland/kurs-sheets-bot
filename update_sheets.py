#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Автоматический парсер курсов валют с banki.ru
Заполняет Google Sheets (по одному листу на валюту)
Запуск: через GitHub Actions по расписанию
"""

import os
import re
import time
import requests
from lxml import html
import gspread
from google.oauth2.service_account import Credentials
import logging

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# --- КОНФИГУРАЦИЯ ---
# Передаются через GitHub Secrets / Environment Variables
SPREADSHEET_ID = os.environ.get("SPREADSHEET_ID")
CURRENCIES_TO_PARSE = os.environ.get(
    "CURRENCIES", 
    "USD,EUR,GBP,CHF,JPY,CNY,AED,TRY,THB,EGP,KZT,BYN"
).split(",")
# ------------------

def init_google_sheets():
    """Инициализация подключения к Google Sheets через сервисный аккаунт"""
    try:
        creds_json = os.environ.get("GOOGLE_CREDENTIALS")
        if not creds_json:
            raise ValueError("Переменная окружения GOOGLE_CREDENTIALS не установлена")
        
        # Парсим JSON из строки (безопаснее чем eval)
        import json
        creds_data = json.loads(creds_json)
        
        creds = Credentials.from_service_account_info(
            creds_data,
            scopes=["https://www.googleapis.com/auth/spreadsheets"]
        )
        client = gspread.authorize(creds)
        logger.info("✅ Авторизация в Google Sheets успешна")
        return client
    except Exception as e:
        logger.error(f"❌ Ошибка авторизации: {e}")
        raise

def get_or_create_sheet(workbook, currency_code):
    """Получает или создаёт лист для валюты"""
    currency_upper = currency_code.upper().strip()
    try:
        sheet = workbook.worksheet(currency_upper)
        logger.info(f"📄 Лист '{currency_upper}' найден")
    except gspread.exceptions.WorksheetNotFound:
        logger.info(f"📄 Лист '{currency_upper}' не найден, создаю...")
        sheet = workbook.add_worksheet(
            title=currency_upper, 
            rows=200, 
            cols=20
        )
        # Настраиваем заголовки сразу
        headers = ["Название банка", f"Покупка {currency_upper}", 
                  f"Продажа {currency_upper}", "Время обновления"]
        sheet.update('A2:D2', [headers])
        # Форматируем заголовки (жирный шрифт)
        sheet.format('A2:D2', {'textFormat': {'bold': True}})
        logger.info(f"✅ Лист '{currency_upper}' создан и настроен")
    return sheet

def parse_banki_ru(currency_code):
    """Парсинг данных с banki.ru для указанной валюты"""
    currency = currency_code.lower().strip()
    currency_upper = currency_code.upper()
    
    url = f"https://www.banki.ru/products/currency/cash/{currency}/moskva/"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1"
    }
    
    try:
        logger.info(f"🌐 Запрос к {url}")
        response = requests.get(url, headers=headers, timeout=15)
        response.raise_for_status()
        
        tree = html.fromstring(response.content)
        
        # Более устойчивый XPath + резервный вариант
        xpath_variants = [
            "//table[contains(@class, 'js-currency-table')]//tbody",
            "//div[@class='currency-table__wrapper']//tbody",
            "/html/body//table//tbody"
        ]
        
        tbody = None
        for xpath in xpath_variants:
            result = tree.xpath(xpath)
            if result:
                tbody = result[0]
                break
        
        if tbody is None:
            logger.warning(f"⚠️ Не найдена таблица для {currency_upper}")
            return []
        
        rows = tbody.xpath("./tr")
        parsed_data = []
        
        for row in rows:
            cells = row.xpath("./th | ./td")
            if len(cells) < 4:
                continue
            
            row_values = []
            for idx, cell in enumerate(cells):
                # Извлекаем текст, убирая лишние пробелы и переносы
                texts = cell.xpath(".//text()")
                clean_texts = [t.strip() for t in texts if t.strip()]
                
                if not clean_texts:
                    row_values.append("")
                    continue
                    
                cell_text = " ".join(clean_texts)
                
                # Очистка для первого столбца (название банка)
                if idx == 0:
                    # Убираем маркеры, переносы, лишние символы
                    cell_text = re.sub(r'[•\-\*]\s*', '', cell_text)
                    cell_text = cell_text.split('\n')[0].strip()
                    # Пропускаем строки, начинающиеся с "1 USD" и т.п.
                    if re.match(r'^\d+\s+[A-Z]{3}', cell_text):
                        continue
                
                row_values.append(cell_text)
            
            # Фильтр: пропускаем строки, где первый элемент — не название банка
            if row_values and row_values[0] and not row_values[0].startswith("1 "):
                # Берём только первые 4 колонки
                parsed_data.append(row_values[:4])
        
        logger.info(f"📊 Для {currency_upper} извлечено {len(parsed_data)} записей")
        return parsed_data
        
    except requests.exceptions.RequestException as e:
        logger.error(f"🌐 Ошибка запроса для {currency_upper}: {e}")
        return []
    except Exception as e:
        logger.error(f"🔍 Ошибка парсинга для {currency_upper}: {e}")
        return []

def update_sheet_data(sheet, data, currency_code):
    """Обновляет данные в листе (начиная с строки 3)"""
    try:
        if not data:
            logger.warning(f"⚠️ Нет данных для записи в лист {currency_code}")
            return False
        
        # Очищаем старые данные (с строки 3 до конца)
        last_row = sheet.row_count
        if last_row > 2:
            sheet.batch_clear([f"A3:D{last_row}"])
        
        # Формируем данные для записи
        rows_to_write = []
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
        
        for row in data:
            # Добавляем время обновления в конец каждой строки
            row_with_time = row + [timestamp] if len(row) == 3 else row[:3] + [timestamp]
            rows_to_write.append(row_with_time)
        
        # Записываем данные
        sheet.update(f"A3:D{len(rows_to_write) + 2}", rows_to_write)
        
        logger.info(f"✅ Лист {currency_code} обновлён: {len(rows_to_write)} записей")
        return True
        
    except Exception as e:
        logger.error(f"❌ Ошибка записи в лист {currency_code}: {e}")
        return False

def main():
    """Точка входа"""
    logger.info("🚀 Запуск парсера курсов валют")
    
    # Проверка конфигурации
    if not SPREADSHEET_ID:
        logger.error("❌ Не указан SPREADSHEET_ID в переменных окружения")
        return
    
    # Инициализация Google Sheets
    try:
        client = init_google_sheets()
        workbook = client.open_by_key(SPREADSHEET_ID)
    except Exception as e:
        logger.error(f"❌ Не удалось подключиться к таблице: {e}")
        return
    
    # Обработка каждой валюты
    success_count = 0
    for currency in CURRENCIES_TO_PARSE:
        currency = currency.strip().upper()
        if not currency:
            continue
            
        logger.info(f"\n{'='*50}")
        logger.info(f"💱 Обработка валюты: {currency}")
        logger.info(f"{'='*50}")
        
        try:
            # Получаем или создаём лист
            sheet = get_or_create_sheet(workbook, currency)
            
            # Парсим данные
            data = parse_banki_ru(currency)
            
            # Обновляем таблицу
            if data and update_sheet_data(sheet, data, currency):
                success_count += 1
            elif not data:
                logger.warning(f"⚠️ Пропущено: нет данных для {currency}")
            
            # Небольшая пауза между запросами (вежливость к серверу)
            time.sleep(2)
            
        except Exception as e:
            logger.error(f"❌ Критическая ошибка для {currency}: {e}")
            continue
    
    # Итоговый отчёт
    total = len([c for c in CURRENCIES_TO_PARSE if c.strip()])
    logger.info(f"\n{'='*50}")
    logger.info(f"🏁 Завершено: {success_count}/{total} валют обработано успешно")
    logger.info(f"{'='*50}")

if __name__ == "__main__":
    main()