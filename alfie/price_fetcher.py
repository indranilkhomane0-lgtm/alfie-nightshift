import pandas as pd
import numpy as np
from datetime import datetime, timedelta

class PriceFetcher:
    def __init__(self):
        self.prices = {}
    
    def generate_realistic_data(self, days=30):
        """Generate BTC-like price data for testing"""
        dates = pd.date_range(start=datetime.now() - timedelta(days=days), periods=days, freq='D')
        np.random.seed(42)
        base_price = 42000
        trend = np.linspace(0, 6000, days)
        noise = np.random.normal(0, 800, days)
        prices = base_price + trend + noise
        
        return pd.DataFrame({
            'timestamp': dates,
            'close': prices,
            'volume': np.random.randint(500, 3000, days)
        })
    
    def get_prices(self, symbol='BTC', days=30):
        """Offline price data"""
        if symbol not in self.prices:
            self.prices[symbol] = self.generate_realistic_data(days)
        return self.prices[symbol]

if __name__ == '__main__':
    f = PriceFetcher()
    data = f.get_prices('BTC', 30)
    print(f"✓ Price data ready: {len(data)} days")
