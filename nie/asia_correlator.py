"""
Asia Correlator
Watches Asian markets and predicts what US stocks will do at open.
Run this at 7 AM IST - Asian markets have moved by then.
"""

import yfinance as yf
import pandas as pd
import json
from datetime import date

# Asian markets we watch
# These are the ones that predict US stocks most reliably
ASIAN_MARKETS = {
    'TSM': 'TSMC - Taiwan chips - predicts US semiconductors',
    '005930.KS': 'Samsung - Korea chips - predicts US tech',
    '^N225': 'Nikkei - Japan overall - predicts US market mood',
    '^HSI': 'Hang Seng - Hong Kong - predicts risk appetite',
    '^BSESN': 'Sensex - India - our home market'
}

# What each Asian move usually means for US stocks
# You will update these over time as you learn the patterns
CORRELATION_RULES = {
    'TSM': {
        'up_big': 'TSMC up 2%+ usually means US semis (NVDA, AMD, INTC) open strong',
        'down_big': 'TSMC down 2%+ usually means US semis open weak',
        'us_stocks_affected': ['NVDA', 'AMD', 'INTC', 'SMH']
    },
    '005930.KS': {
        'up_big': 'Samsung up 2%+ confirms semiconductor strength globally',
        'down_big': 'Samsung down 2%+ adds pressure to US chip stocks',
        'us_stocks_affected': ['NVDA', 'AMD', 'MU']
    },
    '^N225': {
        'up_big': 'Nikkei up 1%+ means risk is ON - US market likely opens positive',
        'down_big': 'Nikkei down 1%+ means risk is OFF - US market likely opens cautious',
        'us_stocks_affected': ['SPY', 'QQQ', 'EWJ']
    },
    '^HSI': {
        'up_big': 'Hang Seng up 1.5%+ means China risk appetite is good',
        'down_big': 'Hang Seng down 1.5%+ means global risk sentiment is weak',
        'us_stocks_affected': ['SPY', 'FXI', 'BABA']
    }
}

def download_clean(ticker):
    """Downloads data and fixes yfinance column format."""
    data = yf.download(
        ticker,
        period='2d',
        interval='1d',
        progress=False
    )
    if isinstance(data.columns, pd.MultiIndex):
        data.columns = data.columns.droplevel(1)
    return data

def check_one_market(ticker, description):
    """Check how one Asian market moved today."""
    try:
        print(f"Checking {description}...")
        data = download_clean(ticker)

        if data.empty or len(data) < 1:
            print(f"  No data available")
            return None

        # Get today's open and close
        latest = data.iloc[-1]
        open_price = float(latest['Open'])
        close_price = float(latest['Close'])

        # Calculate move
        move = (close_price - open_price) / open_price
        move_pct = round(move * 100, 2)

        # Classify the move size
        abs_move = abs(move_pct)
        if abs_move >= 2.0:
            size = 'BIG'
        elif abs_move >= 1.0:
            size = 'MEDIUM'
        else:
            size = 'SMALL'

        direction = 'UP' if move_pct > 0 else 'DOWN'

        # Get the correlation rule if we have one
        rule = None
        if ticker in CORRELATION_RULES:
            if move_pct >= 2.0:
                rule = CORRELATION_RULES[ticker]['up_big']
            elif move_pct <= -2.0:
                rule = CORRELATION_RULES[ticker]['down_big']

        result = {
            'ticker': ticker,
            'description': description,
            'move_percent': move_pct,
            'direction': direction,
            'size': size,
            'us_prediction': rule,
            'date': str(date.today())
        }

        arrow = '↑' if move_pct > 0 else '↓'
        print(f"  {arrow} {move_pct:+.1f}% [{size}]")
        if rule:
            print(f"  → {rule}")

        return result

    except Exception as e:
        print(f"  Error: {e}")
        return None

def run():
    print("=" * 55)
    print("ASIA CORRELATOR")
    print(f"Date: {date.today()}")
    print("=" * 55)

    results = []
    for ticker, description in ASIAN_MARKETS.items():
        result = check_one_market(ticker, description)
        if result:
            results.append(result)
        print()

    # Overall market mood
    if results:
        ups = sum(1 for r in results if r['direction'] == 'UP')
        downs = sum(1 for r in results if r['direction'] == 'DOWN')

        if ups >= 4:
            mood = 'RISK ON - Most Asian markets up. US likely opens positive.'
        elif downs >= 4:
            mood = 'RISK OFF - Most Asian markets down. US likely opens cautious.'
        elif ups > downs:
            mood = 'MILDLY POSITIVE - More green than red in Asia.'
        else:
            mood = 'MILDLY NEGATIVE - More red than green in Asia.'

        print("=" * 55)
        print(f"OVERALL ASIA MOOD: {mood}")
        print("=" * 55)
    else:
        mood = 'NO DATA'

    # Add mood to results
    summary = {
        'date': str(date.today()),
        'markets': results,
        'overall_mood': mood,
        'ups': ups if results else 0,
        'downs': downs if results else 0
    }

    # Save
    today = str(date.today())
    filename = f'reports/data/{today}-asia.json'
    with open(filename, 'w') as f:
        json.dump(summary, f, indent=2)

    print(f"\nSaved to {filename}")
    print("This data will be used in your Night Shift Report.")

if __name__ == "__main__":
    run()
