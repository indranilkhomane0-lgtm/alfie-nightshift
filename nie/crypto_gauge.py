"""
Crypto Risk Gauge
Bitcoin overnight movement tells us how nervous the market is.
Calm BTC = calm US open. Wild BTC = expect a rough or exciting day.
Run this at 6 AM IST.
"""

import yfinance as yf
import pandas as pd
import json
from datetime import date

def download_clean(ticker):
    """Downloads data and fixes yfinance column format."""
    data = yf.download(
        ticker,
        period='1d',
        interval='5m',
        progress=False
    )
    if isinstance(data.columns, pd.MultiIndex):
        data.columns = data.columns.droplevel(1)
    return data

def read_btc():
    """Check how much Bitcoin moved overnight."""
    try:
        print("Checking Bitcoin...")
        data = download_clean('BTC-USD')

        if data.empty:
            print("  No BTC data")
            return None

        high = float(data['High'].max())
        low = float(data['Low'].min())
        first = float(data['Open'].iloc[0])
        last = float(data['Close'].iloc[-1])

        # How wide was the range today
        day_range = (high - low) / low
        # Where did it end vs where it started
        day_move = (last - first) / first

        range_pct = round(day_range * 100, 2)
        move_pct = round(day_move * 100, 2)

        # Classify nervousness
        if range_pct > 5:
            signal = 'VERY NERVOUS'
            us_prediction = 'Expect high volatility at US open. VIX likely rising.'
            emoji = '🔴'
        elif range_pct > 3:
            signal = 'NERVOUS'
            us_prediction = 'Some volatility expected. Watch first 30 mins carefully.'
            emoji = '🟡'
        elif range_pct > 1.5:
            signal = 'MILD'
            us_prediction = 'Normal day expected. No crypto warning signal.'
            emoji = '🟢'
        else:
            signal = 'CALM'
            us_prediction = 'Very calm. Markets likely to open steady.'
            emoji = '🟢'

        direction = 'UP' if day_move > 0 else 'DOWN'

        print(f"  BTC range today: {range_pct:.1f}%")
        print(f"  BTC moved: {move_pct:+.1f}% ({direction})")
        print(f"  Signal: {emoji} {signal}")
        print(f"  → {us_prediction}")

        return {
            'ticker': 'BTC-USD',
            'date': str(date.today()),
            'high': round(high, 0),
            'low': round(low, 0),
            'last_price': round(last, 0),
            'day_range_pct': range_pct,
            'day_move_pct': move_pct,
            'direction': direction,
            'signal': signal,
            'us_prediction': us_prediction
        }

    except Exception as e:
        print(f"  Error: {e}")
        return None

def read_eth():
    """Check Ethereum - confirms or contradicts BTC signal."""
    try:
        print("\nChecking Ethereum...")
        data = download_clean('ETH-USD')

        if data.empty:
            return None

        first = float(data['Open'].iloc[0])
        last = float(data['Close'].iloc[-1])
        move = (last - first) / first
        move_pct = round(move * 100, 2)

        direction = 'UP' if move_pct > 0 else 'DOWN'
        arrow = '↑' if move_pct > 0 else '↓'
        print(f"  ETH moved: {arrow} {move_pct:+.1f}%")

        return {
            'ticker': 'ETH-USD',
            'date': str(date.today()),
            'move_pct': move_pct,
            'direction': direction
        }

    except Exception as e:
        print(f"  Error: {e}")
        return None

def run():
    print("=" * 55)
    print("CRYPTO RISK GAUGE")
    print(f"Date: {date.today()}")
    print("=" * 55)

    btc = read_btc()
    eth = read_eth()

    # Combined read
    print()
    if btc and eth:
        if btc['direction'] == eth['direction']:
            confirmation = f"BTC and ETH moving same direction ({btc['direction']}) - signal is CONFIRMED"
        else:
            confirmation = "BTC and ETH moving opposite directions - signal is MIXED"
        print(f"Confirmation: {confirmation}")

    summary = {
        'date': str(date.today()),
        'btc': btc,
        'eth': eth,
        'confirmation': confirmation if btc and eth else 'INCOMPLETE DATA'
    }

    today = str(date.today())
    filename = f'reports/data/{today}-crypto.json'
    with open(filename, 'w') as f:
        json.dump(summary, f, indent=2)

    print(f"\nSaved to {filename}")

if __name__ == "__main__":
    run()
