#!/usr/bin/env python3
"""
Alfie Night Shift — automated outcome labeler.

Reads reports/predictions.jsonl. For every prediction whose settle_date has
arrived and is still OPEN, fetches that day's daily close from the same
exchange the prediction was made against, grades it by a FIXED rule declared
in advance, flips the row to LABELED, and chains the labeled outcome to the
tamper-evident record.

Grading rule (fixed, never changed retroactively):
    long   -> WIN if settle_close > entry_price, else LOSS
    short  -> WIN if settle_close < entry_price, else LOSS
    neutral-> NO_CALL (the strategy had no entry signal; not counted as a
              win or a loss, and excluded from the meta-model corpus)

NO_CALL exists so the record never claims credit for a call it didn't make.
Run daily; it is idempotent and does nothing until a settle date arrives.
"""

import json
import sys
from datetime import date, datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PRED_PATH = ROOT / "reports" / "predictions.jsonl"


def fetch_settle_close(asset: str, settle_date: str):
    """Daily close for settle_date from Binance (same source as entry)."""
    import ccxt
    ex = ccxt.binance({"enableRateLimit": True})
    since = int(datetime.strptime(settle_date, "%Y-%m-%d")
                .replace(tzinfo=timezone.utc).timestamp() * 1000)
    bars = ex.fetch_ohlcv(asset, "1d", since=since, limit=3)
    for ts, o, h, l, c, v in bars:
        d = datetime.fromtimestamp(ts / 1000, tz=timezone.utc).date().isoformat()
        if d == settle_date:
            return float(c)
    return None


def grade(direction: str, entry: float, settle: float):
    """Fixed rule. Returns (outcome, return_pct_of_the_taken_side)."""
    move = (settle - entry) / entry
    if direction == "long":
        return ("WIN" if settle > entry else "LOSS", move)
    if direction == "short":
        return ("WIN" if settle < entry else "LOSS", -move)
    return ("NO_CALL", move)


def main() -> int:
    if not PRED_PATH.exists():
        print("no predictions file yet — nothing to label")
        return 0

    rows = [json.loads(l) for l in PRED_PATH.read_text().splitlines() if l.strip()]
    today = date.today().isoformat()
    due = [r for r in rows
           if r.get("status") == "OPEN" and r.get("settle_date", "9999") <= today]

    if not due:
        open_n = sum(1 for r in rows if r.get("status") == "OPEN")
        print(f"nothing due today. {open_n} prediction(s) still open.")
        return 0

    sys.path.insert(0, str(ROOT))
    from nightshift.publish_chain import append_entry

    labeled = 0
    for r in due:
        try:
            settle_close = fetch_settle_close(r["asset"], r["settle_date"])
        except Exception as e:
            print(f"price fetch failed for {r['prediction_id']}: {e} — leaving OPEN")
            continue
        if settle_close is None:
            print(f"no bar yet for {r['prediction_id']} @ {r['settle_date']} — leaving OPEN")
            continue

        outcome, ret = grade(r["direction"], r["entry_price"], settle_close)
        r["settle_close"] = round(settle_close, 6)
        r["return_pct"] = round(ret * 100, 4)
        r["outcome"] = outcome
        r["labeled_at_utc"] = datetime.now(timezone.utc).isoformat()
        r["status"] = "LABELED"

        append_entry({
            "type": "LABELED_OUTCOME",
            "prediction_id": r["prediction_id"],
            "asset": r["asset"],
            "config_id": r.get("config_id"),
            "direction": r["direction"],
            "entry_price": r["entry_price"],
            "settle_close": r["settle_close"],
            "return_pct": r["return_pct"],
            "outcome": outcome,
            "cycle_date": r["cycle_date"],
            "settle_date": r["settle_date"],
        })
        labeled += 1
        print(f"labeled {r['prediction_id']}: {r['direction']} "
              f"{r['entry_price']} -> {r['settle_close']} = {outcome} ({r['return_pct']:+.2f}%)")

    PRED_PATH.write_text("".join(json.dumps(r, sort_keys=True) + "\n" for r in rows))

    graded = [r for r in rows if r.get("status") == "LABELED" and r.get("outcome") != "NO_CALL"]
    print(f"\n{labeled} newly labeled. Corpus: {len(graded)}/30 graded rows "
          f"({sum(1 for r in graded if r['outcome']=='WIN')}W/"
          f"{sum(1 for r in graded if r['outcome']=='LOSS')}L)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
