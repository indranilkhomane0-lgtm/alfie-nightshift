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
    code_versions = {}  # code_version -> 1-based index of first appearance
    # (asset, direction, cycle_date) -> outcome. Multiple configs can
    # agree on the same asset/direction/night -- same entry price, same
    # settle price, same graded outcome -- and each still gets its own
    # chained LABELED_OUTCOME entry. Collapsing on this key is what makes
    # the headline number distinct calls made, not configs that agreed.
    # Same key status.py uses, for the same reason.
    distinct_outcomes = {}
    outcome_conflicts = 0  # same key, different outcome across entries -- should never happen

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
            cv = p.get("code_version", "(predates code_version)")
            if cv not in code_versions:
                code_versions[cv] = i
            if p.get("type") == "PIPELINE_FAILURE":
                failures += 1
            outcome = str(p.get("outcome", "")).upper()
            if outcome == "WIN":
                wins += 1
            elif outcome == "LOSS":
                losses += 1
            elif p.get("signal") == "WAIT":
                waits += 1
            if outcome in ("WIN", "LOSS"):
                key = (p.get("asset"), p.get("direction"), p.get("cycle_date"))
                prior = distinct_outcomes.get(key)
                if prior is not None and prior != outcome:
                    outcome_conflicts += 1
                distinct_outcomes[key] = outcome

    distinct_wins = sum(1 for o in distinct_outcomes.values() if o == "WIN")
    distinct_losses = sum(1 for o in distinct_outcomes.values() if o == "LOSS")

    print(f"CHAIN VALID — {i} entries, unbroken from genesis.")
    print(
        f"outcomes on record: {distinct_wins} wins / {distinct_losses} losses "
        f"({distinct_wins + distinct_losses} distinct calls) / "
        f"{waits} waits / {failures} pipeline failures"
    )
    print(
        f"  {wins} win / {losses} loss chain entries (configs-in-agreement, "
        f"same asset+direction+night counted once above)"
    )
    if outcome_conflicts:
        print(
            f"  CONFLICT: {outcome_conflicts} case(s) where configs agreeing on "
            f"asset+direction+night graded differently -- distinct-call counts "
            f"above are not reliable until this is investigated"
        )
    print(f"code versions on record: {len(code_versions)}")
    for cv, idx in sorted(code_versions.items(), key=lambda kv: kv[1]):
        is_sha = cv not in ("unknown", "(predates code_version)")
        label = cv[:12] if is_sha else cv
        print(f"  {label:23s} first appears at entry {idx}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
