import json
from datetime import datetime

class SignalGenerator:
    def generate(self, regime, confidence, prices):
        latest_close = prices['close'].iloc[-1]
        sma_20 = prices['close'].rolling(20).mean().iloc[-1]
        latest_return = prices['close'].iloc[-1] / prices['close'].iloc[-5] - 1
        
        if regime == 'TRENDING':
            if latest_return > 0.01 and confidence > 0.70:
                signal = 'BUY'
            elif latest_return < -0.01:
                signal = 'SELL'
            else:
                signal = 'WAIT'
        elif regime == 'RANGING':
            if latest_close < sma_20 * 0.98 and confidence > 0.75:
                signal = 'BUY'
            elif latest_close > sma_20 * 1.02:
                signal = 'SELL'
            else:
                signal = 'WAIT'
        else:
            signal = 'WAIT'
        
        return {'signal': signal, 'regime': regime, 'confidence': round(confidence, 2), 'timestamp': datetime.now().isoformat(), 'asset': 'BTC/USDT'}

if __name__ == '__main__':
    from price_fetcher import PriceFetcher
    from regime_detector import RegimeDetector
    prices = PriceFetcher().get_prices()
    regime_result = RegimeDetector().detect(prices)
    signal = SignalGenerator().generate(regime_result['regime'], regime_result['confidence'], prices)
    print(f"✓ Signal: {signal['signal']} ({signal['confidence']:.0%} confidence)")
