#!/usr/bin/env python3
"""
Exchange Rate Database – Backfill & Daily Update
Usage:
    python main.py backfill   # loads all historical data (1999–yesterday) from Frankfurter,
                              # saves to SQLite and writes yearly JSON files in data/
    python main.py update     # fetches today's rates from ExchangeRate-API, appends to SQLite
                              # and updates the current year's JSON file
    python main.py full       # backfill first, then update (useful for initial setup)
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

# Ensure data directory exists
DATA_DIR = Path("data")
DATA_DIR.mkdir(exist_ok=True)

# ----------------------------------------------------------------------
# Database layer
# ----------------------------------------------------------------------
engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False} if "sqlite" in DATABASE_URL else {})
SessionLocal = sessionmaker(bind=engine)

def init_db():
    """Create the exchange_rates table if it doesn't exist."""
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
    """Insert rates for a single date into SQLite, skipping duplicates."""
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
# JSON helpers (yearly archives)
# ----------------------------------------------------------------------
def read_year_json(year):
    """Read the existing JSON for a given year, or return empty dict."""
    filepath = DATA_DIR / f"{year}.json"
    if filepath.exists():
        with open(filepath, 'r') as f:
            return json.load(f)
    return {}

def write_year_json(year, data):
    """Write the full year data to data/YYYY.json."""
    filepath = DATA_DIR / f"{year}.json"
    with open(filepath, 'w') as f:
        json.dump(data, f, indent=2, sort_keys=True)
    logger.info(f"Saved {len(data)} dates to {filepath}")

def append_date_to_json(date_obj, rates):
    """Append or update a single date's rates in the current year's JSON."""
    year = date_obj.year
    date_str = date_obj.isoformat()
    year_data = read_year_json(year)
    year_data[date_str] = rates
    write_year_json(year, year_data)

def save_full_year_to_json(year, rates_by_date):
    """Write a complete year's data (dict date_str -> rates) to JSON."""
    write_year_json(year, rates_by_date)

# ----------------------------------------------------------------------
# Data fetchers
# ----------------------------------------------------------------------
def fetch_frankfurter_year(year):
    """Fetch a full year (or up to yesterday) from Frankfurter."""
    start = f"{year}-01-01"
    today = datetime.now().date()
    end = f"{year}-12-31" if year < today.year else (today - timedelta(days=1)).strftime("%Y-%m-%d")
    url = f"https://api.frankfurter.dev/v1/{start}..{end}"
    logger.info(f"Fetching Frankfurter: {url}")
    resp = requests.get(url, timeout=60)
    resp.raise_for_status()
    data = resp.json()
    return data.get('rates', {})  # dict of date_str -> {currency: rate}

def fetch_exchangerate_api():
    """Fetch today's rates from ExchangeRate-API."""
    url = f"https://v6.exchangerate-api.com/v6/{EXCHANGE_API_KEY}/latest/EUR"
    logger.info(f"Fetching ExchangeRate-API: {url}")
    resp = requests.get(url, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    if data.get('result') != 'success':
        raise RuntimeError(f"API error: {data}")
    rates = data.get('conversion_rates', {})
    if 'EUR' in rates:
        del rates['EUR']          # remove the base itself
    update_utc = data.get('time_last_update_utc')
    if update_utc:
        dt = datetime.strptime(update_utc, "%a, %d %b %Y %H:%M:%S %z")
        date_obj = dt.date()
    else:
        date_obj = datetime.now().date()
    return date_obj, rates

# ----------------------------------------------------------------------
# Core actions
# ----------------------------------------------------------------------
def backfill():
    """Load all historical data from Frankfurter (1999 – yesterday)."""
    init_db()
    today = datetime.now().date()
    for year in range(1999, today.year + 1):
        logger.info(f"Backfilling year {year}")
        rates_by_date = fetch_frankfurter_year(year)
        if rates_by_date:
            # Write the entire year to JSON
            save_full_year_to_json(year, rates_by_date)
            # Insert each date into SQLite
            for date_str, rates in rates_by_date.items():
                date_obj = datetime.strptime(date_str, "%Y-%m-%d").date()
                if date_obj > today:
                    continue
                insert_rates(date_obj, rates, source="Frankfurter")
    logger.info("Backfill complete.")

def daily_update():
    """Fetch today's rates from ExchangeRate-API, insert into DB and append to JSON."""
    init_db()
    date_obj, rates = fetch_exchangerate_api()
    # Insert into SQLite
    insert_rates(date_obj, rates, source="ExchangeRate-API")
    # Append to yearly JSON
    append_date_to_json(date_obj, rates)
    logger.info(f"Daily update finished for {date_obj}.")

# ----------------------------------------------------------------------
# CLI entry point
# ----------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="Exchange Rate DB maintenance")
    parser.add_argument("mode", choices=["backfill", "update", "full"],
                        help="backfill: load all historical data; update: fetch today; full: backfill then update")
    args = parser.parse_args()

    if args.mode in ("backfill", "full"):
        backfill()
    if args.mode in ("update", "full"):
        daily_update()

if __name__ == "__main__":
    main()
