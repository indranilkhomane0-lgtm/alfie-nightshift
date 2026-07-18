#!/usr/bin/env python3
"""
Alfie Night Shift — tamper-evident record publisher.

Appends each nightly brief to reports/chain.jsonl as a hash-chained entry:
    entry_hash = sha256(prev_hash + canonical_json(payload))

Any edit to any historical entry breaks every hash after it.
Verification requires nothing but Python stdlib (see verify_chain.py).

Usage (called by the nightly pipeline as its final step):
    python3 nightshift/publish_chain.py --brief nightshift/briefs/brief_YYYYMMDD.txt

Honesty rules enforced here, not by discipline:
  - Losses are published identically to wins (no filtering hook exists).
  - If the pipeline failed, publish a FAILED entry — gaps are suspicious,
    failures are honest.
"""

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

CHAIN_PATH = Path(__file__).resolve().parent.parent / "reports" / "chain.jsonl"
GENESIS_HASH = "0" * 64


def canonical(obj) -> bytes:
    """Deterministic JSON serialization — key order and separators fixed."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":")).encode()


def last_hash() -> str:
    if not CHAIN_PATH.exists() or CHAIN_PATH.stat().st_size == 0:
        return GENESIS_HASH
    with CHAIN_PATH.open("rb") as f:
        last_line = f.read().splitlines()[-1]
    return json.loads(last_line)["entry_hash"]


def append_entry(payload: dict) -> dict:
    prev = last_hash()
    entry = {
        "published_at_utc": datetime.now(timezone.utc).isoformat(),
        "prev_hash": prev,
        "payload": payload,
    }
    entry["entry_hash"] = hashlib.sha256(
        prev.encode() + canonical(payload)
    ).hexdigest()
    CHAIN_PATH.parent.mkdir(parents=True, exist_ok=True)
    with CHAIN_PATH.open("a") as f:
        f.write(json.dumps(entry, sort_keys=True) + "\n")
    return entry


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--report", help="Path to tonight's signal/brief JSON")
    ap.add_argument("--brief", help="Path to tonight's brief .txt (nightshift/briefs/)")
    ap.add_argument(
        "--failed",
        action="store_true",
        help="Publish a pipeline-failure entry instead of a report",
    )
    args = ap.parse_args()

    if args.failed:
        payload = {
            "type": "PIPELINE_FAILURE",
            "note": "Nightly run did not complete. Published for record continuity.",
        }
    elif args.brief:
        text = Path(args.brief).read_text()
        payload = {
            "type": "NIGHTLY_BRIEF",
            "brief_file": Path(args.brief).name,
            "brief_sha256": hashlib.sha256(text.encode()).hexdigest(),
        }
    else:
        if not args.report:
            print("error: --report or --brief required unless --failed", file=sys.stderr)
            return 2
        payload = json.loads(Path(args.report).read_text())
        payload["type"] = payload.get("type", "NIGHTLY_BRIEF")

    entry = append_entry(payload)
    print(f"chained: {entry['entry_hash'][:16]}…  (prev {entry['prev_hash'][:16]}…)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
