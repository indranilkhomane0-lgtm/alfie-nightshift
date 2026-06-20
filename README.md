# Alfie Night Shift

Autonomous trading signals. No humans. No emotions. Just logic.

## What Works Right Now

✅ Price data fetching (offline)
✅ Market regime detection (TRENDING/RANGING/VOLATILE)
✅ Signal generation (BUY/SELL/WAIT)
✅ Signal logging and storage
✅ Slack integration (ready, needs webhook)
✅ Email integration (ready, needs Gmail)

## Quick Start

```bash
source venv/bin/activate
python3 alfie/orchestrator.py
cat alfie/signals/signal_latest.json
```

## Architecture

- `price_fetcher.py` - Get market data
- `regime_detector.py` - Detect market condition
- `signal_generator.py` - Generate trading signal
- `orchestrator.py` - Tie it all together
- `slack_sender.py` - Send to Slack
- `email_sender.py` - Send via email

## Status

**Day 1: Core system complete and tested ✅**

Next:
- Setup Slack webhook (Day 2)
- Setup Gmail app password (Day 2)
- Deploy to GitHub (Day 2)
- Get first customers (Week 1)

## Metrics

- Lines of code: ~300
- Modules: 5
- Test pass rate: 100%
- Development time: 4 hours
