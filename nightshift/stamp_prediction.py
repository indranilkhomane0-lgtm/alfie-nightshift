#!/usr/bin/env python3
"""
Alfie Night Shift — prediction stamp.

Records a concrete, gradeable prediction for every config that passed MC
gating that night AND has a live entry signal today (not just the single
top-ranked config), so the outcome labeler has a fixed claim to grade
HOLD_DAYS later for each of them. A config that passed gating but has no
entry signal produces no row at all -- see the direction == "none" check
in stamp() below -- rather than a row that could only ever grade NO_CALL.
An asset can therefore produce zero, one, or several stamped predictions
on a given night, depending on how many of its gated configs both passed
and are actually signaling entry today.

DIRECTION IS DERIVED FROM THE STRATEGY'S OWN ENTRY CONDITION.
Every strategy in nightshift/strategies is LONG-ONLY (signal is 0.0 or 1.0,
never negative). Therefore entry_signal() can only ever return:
    "long" -> the strategy's entry condition is true today; stamped
    "none" -> no entry signal today; stamp() logs it and returns None,
              writing nothing
It must never emit "short". Alfie does not short, and the record must not
claim a call the system never made.

Predictions stamped before this change (2026-08-08) include "none" and
"neutral" rows written under the old behaviour; label_outcomes.py still
grades those NO_CALL exactly as before -- this only changes what gets
written going forward, not what's already in reports/predictions.jsonl.

Entry conditions mirrored from nightshift/strategies/__init__.py:
    mean_rev : RSI(rsi_period) < rsi_low
    momentum : MA(fast) > MA(slow) AND RSI(14) > rsi_entry
    relative : z(lookback return, 30d) > entry_z
"""

import json
import logging
from datetime import date, timedelta
from pathlib import Path

HOLD_DAYS = 7
PRED_PATH = Path(__file__).resolve().parent.parent / "reports" / "predictions.jsonl"
log = logging.getLogger(__name__)


def _rsi(closes, period=14):
    if len(closes) <= period + 1:
        return None
    gains, losses = [], []
    for a, b in zip(closes[:-1], closes[1:]):
        d = b - a
        gains.append(max(d, 0.0))
        losses.append(max(-d, 0.0))
    g = sum(gains[-period:]) / period
    l = sum(losses[-period:]) / period
    if l == 0:
        return 100.0
    return 100.0 - 100.0 / (1.0 + g / l)


def _ma(closes, n):
    return sum(closes[-n:]) / n if len(closes) >= n else None


def _zscore(closes, lookback, window=30):
    """z of the lookback-period return, standardised over `window` observations."""
    need = lookback + window + 1
    if len(closes) < need:
        return None
    rets = [(closes[i] - closes[i - lookback]) / closes[i - lookback]
            for i in range(len(closes) - window, len(closes))]
    mu = sum(rets) / len(rets)
    var = sum((r - mu) ** 2 for r in rets) / len(rets)
    sd = var ** 0.5
    if sd == 0:
        return None
    return (rets[-1] - mu) / sd


def entry_signal(family: str, params: dict, closes) -> tuple:
    """Returns ("long"|"none", diagnostic dict). Long-only by design."""
    if family == "mean_rev":
        p = int(params.get("rsi_period", 14))
        r = _rsi(closes, p)
        lo = params.get("rsi_low")
        d = {"rsi": round(r, 2) if r is not None else None, "rsi_low": lo}
        return ("long" if (r is not None and lo is not None and r < lo) else "none", d)

    if family == "momentum":
        f, s = int(params.get("fast_ma", 18)), int(params.get("slow_ma", 72))
        re_ = params.get("rsi_entry", 38)
        mf, ms, r = _ma(closes, f), _ma(closes, s), _rsi(closes, 14)
        d = {"ma_fast": mf, "ma_slow": ms, "rsi": round(r, 2) if r else None,
             "rsi_entry": re_}
        ok = None not in (mf, ms, r) and mf > ms and r > re_
        return ("long" if ok else "none", d)

    if family == "relative":
        lb = int(params.get("lookback", 14))
        ez = float(params.get("entry_z", 1.5))
        z = _zscore(closes, lb)
        d = {"z": round(z, 3) if z is not None else None, "entry_z": ez}
        return ("long" if (z is not None and z > ez) else "none", d)

    return ("none", {"note": f"unknown family {family}"})


def stamp(cfg: dict, closes) -> dict | None:
    closes = [float(c) for c in closes]
    params = cfg.get("params", {}) or {}
    family = cfg.get("strategy_family", "")
    direction, diag = entry_signal(family, params, closes)
    if direction == "none":
        log.info("  %s: no entry signal tonight (%s)", cfg.get("config_id"), diag)
        return None
    entry = date.today()
    pred = {
        "prediction_id": f"{cfg['config_id'].replace('/','')}_{entry.isoformat()}",
        "cycle_date": entry.isoformat(),
        "asset": cfg["asset"],
        "config_id": cfg["config_id"],
        "strategy_family": family,
        "direction": direction,          # "long" or "none" — never "short"
        "entry_signal_detail": diag,
        "entry_price": round(closes[-1], 6),
        "hold_days": HOLD_DAYS,
        "settle_date": (entry + timedelta(days=HOLD_DAYS)).isoformat(),
        "wfo_win_rate": cfg.get("wfo_win_rate"),
        "regime_state": cfg.get("regime_state"),
        "wfo_n_trials": cfg.get("wfo_n_trials"),
        "wfo_n_candidates_surviving_min_sharpe": cfg.get("wfo_n_candidates_surviving_min_sharpe"),
        "status": "OPEN",
    }
    PRED_PATH.parent.mkdir(parents=True, exist_ok=True)
    with PRED_PATH.open("a") as f:
        f.write(json.dumps(pred, sort_keys=True) + "\n")
    return pred


if __name__ == "__main__":
    # mean_rev: oversold -> long ; mid-band -> none
    over = [100 - i for i in range(60)]            # falling -> low RSI
    mid = [100 + (i % 3) for i in range(60)]       # flat -> mid RSI
    cfg = {"asset": "ETH/USDT", "config_id": "t", "strategy_family": "mean_rev",
           "params": {"rsi_low": 30, "rsi_high": 70, "rsi_period": 14}}
    print("falling mkt ->", entry_signal("mean_rev", cfg["params"], over))
    print("flat mkt    ->", entry_signal("mean_rev", cfg["params"], mid))
    print("momentum    ->", entry_signal("momentum",
          {"fast_ma": 5, "slow_ma": 20, "rsi_entry": 38}, [100 + i for i in range(60)]))
