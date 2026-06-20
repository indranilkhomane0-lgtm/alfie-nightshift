import json
from datetime import datetime
from price_fetcher import PriceFetcher
from regime_detector import RegimeDetector
from signal_generator import SignalGenerator

class Orchestrator:
    def __init__(self):
        self.fetcher = PriceFetcher()
        self.detector = RegimeDetector()
        self.generator = SignalGenerator()
    
    def run(self):
        try:
            prices = self.fetcher.get_prices('BTC', 30)
            regime_result = self.detector.detect(prices)
            signal = self.generator.generate(regime_result['regime'], regime_result['confidence'], prices)
            
            signal_file = 'alfie/signals/signal_latest.json'
            with open(signal_file, 'w') as f:
                json.dump(signal, f, indent=2)
            
            log_file = 'alfie/logs/run.log'
            with open(log_file, 'a') as f:
                f.write(f"{datetime.now().isoformat()} - {signal['signal']} ({signal['confidence']:.0%})\n")
            
            return signal
        except Exception as e:
            print(f"ERROR: {e}")
            return None

if __name__ == '__main__':
    signal = Orchestrator().run()
    if signal:
        print(f"✓ Signal generated: {signal['signal']}")
        print(f"✓ Saved to: alfie/signals/signal_latest.json")
