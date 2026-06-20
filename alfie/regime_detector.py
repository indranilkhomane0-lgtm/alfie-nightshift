import numpy as np
import pandas as pd

class RegimeDetector:
    def __init__(self, lookback=20):
        self.lookback = lookback
    
    def detect(self, prices):
        df = prices.copy()
        df['returns'] = df['close'].pct_change()
        df['volatility'] = df['returns'].rolling(self.lookback).std()
        df['trend'] = (df['close'] - df['close'].rolling(self.lookback).mean()) / df['close'].rolling(self.lookback).mean()
        df['range'] = (df['close'].rolling(self.lookback).max() - df['close'].rolling(self.lookback).min()) / df['close'].iloc[0]
        latest = df.iloc[-1]
        vol_threshold = 0.025
        trend_threshold = 0.01
        if abs(latest['trend']) > trend_threshold and latest['volatility'] < vol_threshold:
            regime = 'TRENDING'
            confidence = min(0.7 + abs(latest['trend']) * 2, 0.95)
        elif latest['volatility'] > vol_threshold:
            regime = 'VOLATILE'
            confidence = min(0.60 + latest['volatility'] * 5, 0.90)
        else:
            regime = 'RANGING'
            confidence = 0.80
        return {'regime': regime, 'confidence': confidence, 'volatility': latest['volatility'], 'trend': latest['trend']}

if __name__ == '__main__':
    from price_fetcher import PriceFetcher
    prices = PriceFetcher().get_prices()
    result = RegimeDetector().detect(prices)
    print(f"✓ Regime: {result['regime']} ({result['confidence']:.0%})")
