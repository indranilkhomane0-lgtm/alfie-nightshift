"""
After Hours Tracker
Watches what happens to stocks after US market closes at 4 PM ET.
Run this between 8:30 PM - 11 PM IST.
"""

import yfinance as yf
import pandas as pd
import json
from datetime import date

WATCHLIST = ['AAPL', 'NVDA', 'TSLA', 'AMD', 'META']

def download_clean(ticker):
    """Downloads stock data and fixes yfinance column format."""
    data = yf.download(
        ticker,
        period='2d',
        interval='1m',
        prepost=True,
        progress=False
    )
    if isinstance(data.columns, pd.MultiIndex):
        data.columns = data.columns.droplevel(1)
    return data

def classify_pattern(move_pct):
    if move_pct > 0.05:
        return "EUPHORIA_FADE_CANDIDATE"
    elif move_pct > 0.03:
        return "MODERATE_UPSIDE"
    elif move_pct < -0.05:
        return "EXTREME_DROP"
    elif move_pct < -0.03:
        return "PANIC_RECOVERY_CANDIDATE"
    else:
        return "FOLLOW_THROUGH_CANDIDATE"

def track_one_stock(ticker):
    try:
        print(f"Checking {ticker}...")
        data = download_clean(ticker)

        if data.empty:
            print(f"  No data for {ticker}")
            return None

        regular = data.between_time('09:30', '15:59')
        if regular.empty:
            return None
        regular_close = float(regular['Close'].iloc[-1])

        after_hours = data.between_time('16:01', '20:00')
        if after_hours.empty:
            print(f"  No after hours data for {ticker} yet (market may be closed)")
            return None
        ah_price = float(after_hours['Close'].iloc[-1])

        move = (ah_price - regular_close) / regular_close
        move_pct_display = round(move * 100, 2)
        pattern = classify_pattern(move)

        result = {
            'ticker': ticker,
            'date': str(date.today()),
            'regular_close': round(regular_close, 2),
            'after_hours_price': round(ah_price, 2),
            'move_percent': move_pct_display,
            'pattern': pattern
        }

        print(f"  {ticker}: {move_pct_display:+.1f}% -> {pattern}")
        return result

    except Exception as e:
        print(f"  Error with {ticker}: {e}")
        return None

def run():
    print("=" * 50)
    print("AFTER HOURS TRACKER")
    print(f"Date: {date.today()}")
    print("=" * 50)

    results = []
    for ticker in WATCHLIST:
        result = track_one_stock(ticker)
        if result:
            results.append(result)

    today = str(date.today())
    filename = f'reports/data/{today}-after-hours.json'

    with open(filename, 'w') as f:
        json.dump(results, f, indent=2)

    print()
    print(f"Saved {len(results)} stocks to {filename}")

if __name__ == "__main__":
    run()
