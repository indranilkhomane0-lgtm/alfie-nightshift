#!/usr/bin/env python3
"""
Alfie Night Shift — void a batch of predictions and corpus rows.

For nights whose inputs are known-bad after the fact (e.g. the
2026-07-31/08-01 synthetic-price incident), this marks the affected
reports/predictions.jsonl rows VOID instead of deleting them, excludes
the matching nightshift/corpus.db rows from the meta-model's training
corpus permanently (nightshift/db.py's void_corpus_entries — covers
them even once a live-monitor "survived" label would otherwise land),
and chains a VOID_DECISION entry so the exclusion is a recorded fact,
not a silent absence.

Only rows still OPEN are touched — an already-LABELED row is left
alone and reported, never silently overwritten, since that would erase
a real (if contaminated) outcome rather than annotate it.

Usage:
    python3 nightshift/void_predictions.py \\
        --cycle-dates 2026-07-31 2026-08-01 \\
        --reason "synthetic price data (Binance outage) -- see METHODOLOGY_CHANGE"
"""
import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
# Resolved before any file is touched -- if these imports fail, they must
# fail here, not after predictions.jsonl has already been rewritten and
# left the corpus/chain steps undone.
from nightshift.db import init_db, void_corpus_entries
from nightshift.publish_chain import append_entry

PRED_PATH = ROOT / "reports" / "predictions.jsonl"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cycle-dates", nargs="+", required=True,
                     help="One or more cycle_date values (YYYY-MM-DD) to void")
    ap.add_argument("--reason", required=True, help="Explicit void reason, chained verbatim")
    args = ap.parse_args()
    target_dates = set(args.cycle_dates)

    if not PRED_PATH.exists():
        print("no predictions file — nothing to void", file=sys.stderr)
        return 1

    rows = [json.loads(l) for l in PRED_PATH.read_text().splitlines() if l.strip()]
    voided_at = datetime.now(timezone.utc).isoformat()

    voided_prediction_ids, skipped_not_open = [], []
    for r in rows:
        if r.get("cycle_date") not in target_dates:
            continue
        if r.get("status") != "OPEN":
            skipped_not_open.append(r["prediction_id"])
            continue
        r["status"] = "VOID"
        r["void_reason"] = args.reason
        r["voided_at_utc"] = voided_at
        voided_prediction_ids.append(r["prediction_id"])

    if not voided_prediction_ids:
        print("no OPEN predictions matched — nothing to void")
        if skipped_not_open:
            print(f"({len(skipped_not_open)} non-OPEN rows for these dates left untouched: "
                  f"already LABELED or already VOID)")
        return 0

    PRED_PATH.write_text("".join(json.dumps(r, sort_keys=True) + "\n" for r in rows))

    init_db()
    n_corpus_voided = void_corpus_entries(sorted(target_dates), args.reason)

    entry = append_entry({
        "type": "VOID_DECISION",
        "reason": args.reason,
        "cycle_dates": sorted(target_dates),
        "prediction_ids": sorted(voided_prediction_ids),
        "n_predictions_voided": len(voided_prediction_ids),
        "n_corpus_rows_voided": n_corpus_voided,
        "excluded_from": ["outcome_grading", "meta_model_training_corpus"],
        "voided_at_utc": voided_at,
    })

    print(f"voided {len(voided_prediction_ids)} prediction(s) and "
          f"{n_corpus_voided} corpus row(s) for {sorted(target_dates)}")
    if skipped_not_open:
        print(f"left {len(skipped_not_open)} non-OPEN row(s) untouched (already "
              f"LABELED or already VOID): {skipped_not_open}")
    print(f"chained: {entry['entry_hash'][:16]}…  (prev {entry['prev_hash'][:16]}…)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
