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
import hashlib
import logging
import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from core.bar_calendar import is_utc_daily_bar_complete

HOLD_DAYS = 7
PRED_PATH = ROOT / "reports" / "predictions.jsonl"
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


def _context_hash(ohlcv, source, fetched_at) -> str:
    """SHA-256 over the exact OHLCV window used for this stamp, plus the
    data source identifier and fetch timestamp when known. Lets a reader
    verify the INPUT series a prediction was made against -- not just
    that the prediction itself was committed before settlement -- by
    re-fetching the same historical window from `source` and re-hashing
    it the same way. Caller is responsible for passing only CLOSED bars
    (see stamp()) -- this function hashes whatever it's given verbatim.
    Same canonical-JSON + sha256 pattern as wfo_engine._config_id():
    sort_keys + compact separators, so the same window always hashes to
    the same value regardless of dict ordering. Reproducing the hash
    independently requires the source to still serve that exact
    historical range unchanged -- the same caveat that already applies
    to re-fetching for verify_chain.py or the .ots anchors, not a new
    one."""
    idx = [i.isoformat() if hasattr(i, "isoformat") else str(i) for i in ohlcv.index]
    payload = {
        "index": idx,
        "open": ohlcv["open"].tolist(), "high": ohlcv["high"].tolist(),
        "low": ohlcv["low"].tolist(), "close": ohlcv["close"].tolist(),
        "volume": ohlcv["volume"].tolist(),
        "source": source, "fetched_at": fetched_at,
    }
    canon = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canon.encode()).hexdigest()


def stamp(cfg: dict, ohlcv, source: str | None = None,
          fetched_at: str | None = None) -> dict | None:
    closes = ohlcv["close"].tolist()   # full series, live bar included -- signal math is unaffected
    params = cfg.get("params", {}) or {}
    family = cfg.get("strategy_family", "")
    direction, diag = entry_signal(family, params, closes)
    if direction == "none":
        log.info("  %s: no entry signal tonight (%s)", cfg.get("config_id"), diag)
        return None
    entry = date.today()

    # context_hash covers only CLOSED bars. ohlcv's last row is today's
    # in-progress daily candle -- its OHLC keeps changing until the UTC
    # day ends, so hashing it would make context_hash permanently
    # unreproducible: a reader re-fetching later gets a different final
    # bar and therefore a different hash. is_utc_daily_bar_complete()
    # (core/bar_calendar.py) enforces the identical boundary
    # label_outcomes.fetch_settle_close() already uses for grading, so
    # the two can't disagree about what's "final."
    closed = ohlcv[[is_utc_daily_bar_complete(i) for i in ohlcv.index]]

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
        "context_hash": _context_hash(closed, source, fetched_at),
        "context_source": source,
        "context_start": closed.index[0].isoformat() if len(closed) else None,
        "context_end": closed.index[-1].isoformat() if len(closed) else None,
        "context_n_bars": len(closed),
        "context_fetched_at": fetched_at,
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
