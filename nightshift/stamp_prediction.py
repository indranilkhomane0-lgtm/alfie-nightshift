#!/usr/bin/env python3
"""
Alfie Night Shift — prediction stamp.

Records a concrete, gradeable prediction for the night's top config so that
HOLD_DAYS later the outcome labeler has a fixed claim to grade. Without this,
the meta-model's 30-row corpus can never fill: you cannot label what was
never recorded as a fixed prediction.

Appended to reports/predictions.jsonl (plain, greppable, one row per night).
This is the *input* to the labeler; the labeled OUTCOME is what gets chained.
"""

import json
from datetime import date, timedelta
from pathlib import Path

HOLD_DAYS = 7  # fixed rule: matches strategy max_hold_days
PRED_PATH = Path(__file__).resolve().parent.parent / "reports" / "predictions.jsonl"


def compute_rsi(closes, period: int = 14):
    """Wilder's RSI from a list/Series of closes. Returns last value or None."""
    closes = list(map(float, closes))
    if len(closes) <= period:
        return None
    gains, losses = [], []
    for a, b in zip(closes[:-1], closes[1:]):
        d = b - a
        gains.append(max(d, 0.0))
        losses.append(max(-d, 0.0))
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def _direction(params: dict, last_rsi):
    """
    Mean-reversion, conservative:
      RSI <= rsi_low  -> oversold  -> LONG (expect bounce)
      RSI >= rsi_high -> overbought-> SHORT (expect fade)
      else / unknown  -> NEUTRAL (grade on realized move magnitude, no guessed side)
    Never invents a side the strategy didn't clearly imply.
    """
    if last_rsi is None:
        return "neutral"
    low, high = params.get("rsi_low"), params.get("rsi_high")
    if low is not None and last_rsi <= low:
        return "long"
    if high is not None and last_rsi >= high:
        return "short"
    return "neutral"


def stamp(top_cfg: dict, closes) -> dict:
    """
    top_cfg: top config dict from the cycle (asset, strategy_family, params,
             wfo_win_rate, regime_state, config_id, ...)
    closes:  iterable of daily closes for top_cfg['asset'] (prices[asset]['close'])
    """
    closes = list(map(float, closes))
    entry_price = closes[-1]
    period = int(top_cfg.get("params", {}).get("rsi_period", 14))
    last_rsi = compute_rsi(closes, period)
    entry = date.today()
    pred = {
        "prediction_id": f"{top_cfg['asset'].replace('/','')}_{entry.isoformat()}",
        "cycle_date": entry.isoformat(),
        "asset": top_cfg["asset"],
        "config_id": top_cfg.get("config_id"),
        "strategy_family": top_cfg.get("strategy_family"),
        "direction": _direction(top_cfg.get("params", {}), last_rsi),
        "entry_rsi": round(last_rsi, 2) if last_rsi is not None else None,
        "entry_price": round(entry_price, 6),
        "hold_days": HOLD_DAYS,
        "settle_date": (entry + timedelta(days=HOLD_DAYS)).isoformat(),
        "wfo_win_rate": top_cfg.get("wfo_win_rate"),
        "regime_state": top_cfg.get("regime_state"),
        "status": "OPEN",
    }
    PRED_PATH.parent.mkdir(parents=True, exist_ok=True)
    with PRED_PATH.open("a") as f:
        f.write(json.dumps(pred, sort_keys=True) + "\n")
    return pred


if __name__ == "__main__":
    import random
    random.seed(7)
    closes = [3000 + i * 5 + random.uniform(-40, 40) for i in range(90)]  # uptrend -> high RSI
    demo = {
        "asset": "ETH/USDT", "config_id": "ETH/USDT_mean_rev_0003",
        "strategy_family": "mean_rev",
        "params": {"rsi_low": 15, "rsi_high": 60, "rsi_period": 18, "max_hold_days": 7},
        "wfo_win_rate": 0.52, "regime_state": 1,
    }
    print("stamped:", json.dumps(stamp(demo, closes), indent=2))
