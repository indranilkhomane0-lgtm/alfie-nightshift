#!/usr/bin/env python3
"""
Alfie Night Shift — chain verifier.

Anyone can run this against reports/chain.jsonl with zero dependencies:

    python3 verify_chain.py

It recomputes every hash from genesis. If any historical entry was edited,
deleted, or reordered, verification fails at that exact line.
This script is the product's honesty claim, made executable.
"""

import hashlib
import json
import sys
from pathlib import Path

CHAIN_PATH = Path(__file__).resolve().parent.parent / "reports" / "chain.jsonl"
GENESIS_HASH = "0" * 64


def canonical(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":")).encode()


def main() -> int:
    if not CHAIN_PATH.exists():
        print("no chain file found at", CHAIN_PATH)
        return 1

    prev = GENESIS_HASH
    wins = losses = waits = failures = 0

    with CHAIN_PATH.open() as f:
        for i, line in enumerate(f, 1):
            entry = json.loads(line)
            if entry["prev_hash"] != prev:
                print(f"BROKEN at line {i}: prev_hash mismatch")
                return 1
            expect = hashlib.sha256(
                prev.encode() + canonical(entry["payload"])
            ).hexdigest()
            if entry["entry_hash"] != expect:
                print(f"BROKEN at line {i}: entry_hash mismatch (edited payload)")
                return 1
            prev = entry["entry_hash"]

            p = entry["payload"]
            if p.get("type") == "PIPELINE_FAILURE":
                failures += 1
            outcome = str(p.get("outcome", "")).upper()
            if outcome == "WIN":
                wins += 1
            elif outcome == "LOSS":
                losses += 1
            elif p.get("signal") == "WAIT":
                waits += 1

    print(f"CHAIN VALID — {i} entries, unbroken from genesis.")
    print(
        f"outcomes on record: {wins} wins / {losses} losses / "
        f"{waits} waits / {failures} pipeline failures"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
