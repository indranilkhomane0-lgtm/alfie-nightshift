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

A row can also carry status VOID instead of OPEN/LABELED -- set by
nightshift/void_predictions.py when a prediction's inputs are later found
to be invalid (e.g. entry price read from fabricated data). VOID rows are
never selected below: the "still OPEN" filter excludes them the same way
it excludes anything already LABELED. They stay in the file with an
explicit void_reason rather than being deleted.
"""

import json
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PRED_PATH = ROOT / "reports" / "predictions.jsonl"


def fetch_settle_close(asset: str, settle_date: str):
    """FINAL daily close for settle_date from Binance (same source as entry).

    Binance returns the IN-PROGRESS daily candle for the current UTC day,
    whose "close" is just the last trade so far. Accepting that would grade a
    prediction against a partial-day price while calling it the close.
    Measured 2026-08-02 10:23 UTC: asking for that same day returned 63172.0
    — a mid-day price, not a close.

    That mattered concretely: the nightly run fires at 00:00 UTC, and the
    due-check used settle_date <= today, so a prediction settling today would
    have been graded roughly five seconds into its own settle day — i.e.
    against that day's OPEN — and chained permanently as a WIN or LOSS.

    A daily bar is only final once the next UTC day has begun. Return None
    until then; the caller leaves the prediction OPEN and retries tomorrow.
    """
    import ccxt
    bar_start = (datetime.strptime(settle_date, "%Y-%m-%d")
                 .replace(tzinfo=timezone.utc))
    if datetime.now(timezone.utc) < bar_start + timedelta(days=1):
        return None  # settle day still in progress — no final close exists yet
    ex = ccxt.binance({"enableRateLimit": True})
    since = int(bar_start.timestamp() * 1000)
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
    # STRICTLY before today, not <=. A settle_date equal to today has not
    # finished, so its daily close does not exist yet -- see the note in
    # fetch_settle_close(). Grading is therefore due the day AFTER the settle
    # date. fetch_settle_close() enforces the same rule independently, so a
    # mistake in either place cannot produce a partial-day grade on its own.
    today = datetime.now(timezone.utc).date().isoformat()
    due = [r for r in rows
           if r.get("status") == "OPEN" and r.get("settle_date", "9999") < today]

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
