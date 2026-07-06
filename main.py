#!/usr/bin/env python3
"""
Exchange Rate Database – Backfill & Daily Update
Usage:
    python main.py backfill   # reads alldata.json (if present) and splits into yearly JSON files in data/
    python main.py update     # fetches today's rates from ExchangeRate-API and appends to current year's JSON
    python main.py full       # backfill then update (for initial setup)
"""

import os
import sys
import json
import argparse
import logging
import requests
from datetime import datetime, timedelta
from pathlib import Path
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from dotenv import load_dotenv

# ----------------------------------------------------------------------
# Configuration & Logging
# ----------------------------------------------------------------------
load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///exchange_rates.db")
EXCHANGE_API_KEY = os.getenv("EXCHANGE_RATE_API_KEY")
if not EXCHANGE_API_KEY:
    raise ValueError("EXCHANGE_RATE_API_KEY environment variable not set")

DATA_DIR = Path("data")
DATA_DIR.mkdir(exist_ok=True)

# ----------------------------------------------------------------------
# Database layer
# ----------------------------------------------------------------------
engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False} if "sqlite" in DATABASE_URL else {})
SessionLocal = sessionmaker(bind=engine)

def init_db():
    with engine.connect() as conn:
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS exchange_rates (
                date DATE NOT NULL,
                base_currency CHAR(3) NOT NULL DEFAULT 'EUR',
                target_currency CHAR(3) NOT NULL,
                rate NUMERIC(20, 10) NOT NULL,
                source VARCHAR(50) NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (date, base_currency, target_currency)
            );
        """))
        conn.commit()
    logger.info("Database table ready.")

def insert_rates(date_obj, rates_dict, source):
    if not rates_dict:
        return
    with SessionLocal() as session:
        for target, rate in rates_dict.items():
            session.execute(
                text("""
                    INSERT INTO exchange_rates (date, base_currency, target_currency, rate, source)
                    VALUES (:date, 'EUR', :target, :rate, :source)
                    ON CONFLICT (date, base_currency, target_currency) DO NOTHING
                """),
                {"date": date_obj, "target": target, "rate": rate, "source": source}
            )
        session.commit()
    logger.info(f"Inserted {len(rates_dict)} rates for {date_obj} from {source}")

# ----------------------------------------------------------------------
# JSON helpers
# ----------------------------------------------------------------------
def read_year_json(year):
    filepath = DATA_DIR / f"{year}.json"
    if filepath.exists():
        with open(filepath, 'r') as f:
            return json.load(f)
    return {}

def write_year_json(year, data):
    filepath = DATA_DIR / f"{year}.json"
    with open(filepath, 'w') as f:
        json.dump(data, f, indent=2, sort_keys=True)
    logger.info(f"Saved {len(data)} dates to {filepath}")

def append_date_to_json(date_obj, rates):
    year = date_obj.year
    date_str = date_obj.isoformat()
    year_data = read_year_json(year)
    year_data[date_str] = rates
    write_year_json(year, year_data)

# ----------------------------------------------------------------------
# Backfill from local alldata.json
# ----------------------------------------------------------------------
def backfill():
    """Read alldata.json and split into yearly JSON files."""
    alldata_path = Path("alldata.json")
    if not alldata_path.exists():
        logger.error("alldata.json not found – please place it in the current directory.")
        sys.exit(1)

    with open(alldata_path, 'r') as f:
        alldata = json.load(f)

    rates_by_date = alldata.get('rates', {})
    if not rates_by_date:
        logger.error("No 'rates' key found in alldata.json")
        sys.exit(1)

    init_db()

    # Group by year
    year_groups = {}
    for date_str, rates in rates_by_date.items():
        try:
            date_obj = datetime.strptime(date_str, "%Y-%m-%d").date()
        except ValueError:
            logger.warning(f"Skipping invalid date: {date_str}")
            continue
        year = date_obj.year
        year_groups.setdefault(year, {})[date_str] = rates

    # Write each year's file and insert into DB
    for year, year_data in year_groups.items():
        write_year_json(year, year_data)
        for date_str, rates in year_data.items():
            date_obj = datetime.strptime(date_str, "%Y-%m-%d").date()
            insert_rates(date_obj, rates, source="Frankfurter (from alldata)")

    logger.info(f"Backfill complete – processed {len(year_groups)} years.")

# ----------------------------------------------------------------------
# Daily update from ExchangeRate-API
# ----------------------------------------------------------------------
def fetch_exchangerate_api():
    url = f"https://v6.exchangerate-api.com/v6/{EXCHANGE_API_KEY}/latest/EUR"
    logger.info(f"Fetching ExchangeRate-API: {url}")
    resp = requests.get(url, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    if data.get('result') != 'success':
        raise RuntimeError(f"API error: {data}")
    rates = data.get('conversion_rates', {})
    if 'EUR' in rates:
        del rates['EUR']
    update_utc = data.get('time_last_update_utc')
    if update_utc:
        dt = datetime.strptime(update_utc, "%a, %d %b %Y %H:%M:%S %z")
        date_obj = dt.date()
    else:
        date_obj = datetime.now().date()
    return date_obj, rates

def daily_update():
    """Fetch today's rates and append to the current year's JSON and DB."""
    init_db()
    date_obj, rates = fetch_exchangerate_api()
    insert_rates(date_obj, rates, source="ExchangeRate-API")
    append_date_to_json(date_obj, rates)
    logger.info(f"Daily update finished for {date_obj}.")

# ----------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="Exchange Rate DB maintenance")
    parser.add_argument("mode", choices=["backfill", "update", "full"],
                        help="backfill: split alldata.json into yearly JSON; update: fetch today; full: backfill then update")
    args = parser.parse_args()

    if args.mode in ("backfill", "full"):
        backfill()
    if args.mode in ("update", "full"):
        daily_update()

if __name__ == "__main__":
    main()
