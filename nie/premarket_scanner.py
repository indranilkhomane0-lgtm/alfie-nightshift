"""
Premarket Scanner
Watches for unusual volume in US stocks before market opens.
High volume before open = someone knows something.
Run this from 1:30 PM IST onwards (4 AM ET premarket starts).
"""

import yfinance as yf
import pandas as pd
import json
from datetime import date

# Stocks we watch every day
# These are the ones Alfie watches and our clients care about
WATCHLIST = [
    'AAPL', 'NVDA', 'AMD', 'TSLA', 'META',
    'MSFT', 'AMZN', 'GOOGL', 'SPY', 'QQQ'
]

def download_clean(ticker):
    """Downloads data and fixes yfinance column format."""
    data = yf.download(
        ticker,
        period='5d',
        interval='1m',
        prepost=True,
        progress=False
    )
    if isinstance(data.columns, pd.MultiIndex):
        data.columns = data.columns.droplevel(1)
    return data

def scan_one_stock(ticker):
    """Check if a stock has unusual premarket activity."""
    try:
        data = download_clean(ticker)

        if data.empty:
            return None

        # Get previous close (yesterday 4 PM)
        regular = data.between_time('09:30', '16:00')
        if len(regular) < 2:
            return None

        # Get yesterday's close
        prev_close = float(regular['Close'].iloc[-1])

        # Get today's premarket (4 AM to 9:30 AM ET)
        premarket = data.between_time('04:00', '09:29')
        if premarket.empty:
            return None

        pm_volume = int(premarket['Volume'].sum())
        pm_last = float(premarket['Close'].iloc[-1])
        pm_move = (pm_last - prev_close) / prev_close
        pm_move_pct = round(pm_move * 100, 2)

        # Calculate average premarket volume from last 4 days
        all_days = []
        for i in range(1, 5):
            try:
                day_pm = premarket.copy()
                day_vol = int(day_pm['Volume'].sum())
                if day_vol > 0:
                    all_days.append(day_vol)
            except:
                pass

        avg_pm_volume = pm_volume  # fallback
        if all_days:
            avg_pm_volume = sum(all_days) // len(all_days)

        # How unusual is today's volume
        if avg_pm_volume > 0:
            volume_ratio = pm_volume / avg_pm_volume
        else:
            volume_ratio = 1.0

        # Classify
        if volume_ratio >= 3.0:
            verdict = 'VERY UNUSUAL - Someone knows something'
            flag = '🔴'
        elif volume_ratio >= 2.0:
            verdict = 'UNUSUAL - Worth watching'
            flag = '🟡'
        elif volume_ratio >= 1.5:
            verdict = 'SLIGHTLY ELEVATED - Monitor'
            flag = '🔵'
        else:
            verdict = 'NORMAL'
            flag = '⚪'

        result = {
            'ticker': ticker,
            'date': str(date.today()),
            'prev_close': round(prev_close, 2),
            'premarket_price': round(pm_last, 2),
            'premarket_move_pct': pm_move_pct,
            'premarket_volume': pm_volume,
            'volume_ratio': round(volume_ratio, 1),
            'verdict': verdict,
            'flag': flag
        }

        # Only print the interesting ones
        if volume_ratio >= 1.5 or abs(pm_move_pct) > 1.0:
            arrow = '↑' if pm_move_pct > 0 else '↓'
            print(f"  {flag} {ticker}: {pm_move_pct:+.1f}% | Volume {volume_ratio:.1f}x normal")
            print(f"     → {verdict}")

        return result

    except Exception as e:
        return None

def run():
    print("=" * 55)
    print("PREMARKET SCANNER")
    print(f"Date: {date.today()}")
    print("=" * 55)
    print("Scanning for unusual activity...\n")

    results = []
    normal_count = 0

    for ticker in WATCHLIST:
        result = scan_one_stock(ticker)
        if result:
            results.append(result)
            if result['volume_ratio'] < 1.5 and abs(result['premarket_move_pct']) <= 1.0:
                normal_count += 1

    # Summary
    unusual = [r for r in results if r['volume_ratio'] >= 2.0]
    watch = [r for r in results if 1.5 <= r['volume_ratio'] < 2.0]

    print(f"\n{'=' * 55}")
    print(f"SUMMARY")
    print(f"{'=' * 55}")
    print(f"Stocks scanned: {len(results)}")
    print(f"Unusual activity: {len(unusual)}")
    print(f"Worth monitoring: {len(watch)}")
    print(f"Normal: {normal_count}")

    if not unusual and not watch:
        print("\n✅ No unusual premarket activity today.")
        print("   Quiet open expected.")
    elif unusual:
        print(f"\n⚠️  {len(unusual)} stock(s) showing unusual activity.")
        print("   Cross-check with Alfie signals before acting.")

    summary = {
        'date': str(date.today()),
        'stocks_scanned': len(results),
        'unusual_count': len(unusual),
        'results': results,
        'overall': 'UNUSUAL ACTIVITY DETECTED' if unusual else 'NORMAL'
    }

    today = str(date.today())
    filename = f'reports/data/{today}-premarket.json'
    with open(filename, 'w') as f:
        json.dump(summary, f, indent=2)

    print(f"\nSaved to {filename}")

if __name__ == "__main__":
    run()
