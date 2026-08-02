#!/usr/bin/env python3
"""
Alfie Night Shift — status.

Prints the five numbers that define the trajectory, plus whether the local
record has actually reached the remote. Read-only: touches nothing, chains
nothing, never writes. Safe to run any time, including mid-cycle.

    python3 nightshift/status.py

Deliberately prints numbers and no adjectives. "How's Alfie doing" is
answered by the counts or not at all.
"""
import json
import os
import sqlite3
import subprocess
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CHAIN = ROOT / "reports" / "chain.jsonl"
PREDS = ROOT / "reports" / "predictions.jsonl"
DB = ROOT / "nightshift" / "corpus.db"
OTS = ROOT / "reports" / "ots"
AUDIT = ROOT / "reports" / "audit"


def _git(*args):
    try:
        return subprocess.run(["git", *args], cwd=ROOT, capture_output=True,
                              text=True, timeout=15).stdout.strip()
    except Exception:
        return ""


def main():
    if not CHAIN.exists():
        print("no chain at", CHAIN)
        return 1

    entries = [json.loads(l) for l in CHAIN.open() if l.strip()]
    types = Counter(e["payload"].get("type") for e in entries)

    preds = []
    if PREDS.exists():
        preds = [json.loads(l) for l in PREDS.open() if l.strip()]
    outcomes = Counter(p.get("outcome") or "OPEN" for p in preds)
    settled_dates = {p.get("settle_date") for p in preds
                     if p.get("outcome") in ("WIN", "LOSS")}

    # 1. chain length
    print(f"1. chain length ............... {len(entries)} entries")

    # 2. labeled outcome rows -> meta-model graduation
    print(f"2. labeled settle dates ....... {len(settled_dates)} of 30"
          f"   (WIN {outcomes.get('WIN',0)} / LOSS {outcomes.get('LOSS',0)}"
          f" / NO_CALL {outcomes.get('NO_CALL',0)} / open {outcomes.get('OPEN',0)})")

    # 3. regimes survived with losses published
    print(f"3. losses published ........... {outcomes.get('LOSS',0)}")

    # 4. pipeline failures honestly published
    print(f"4. pipeline failures published  {types.get('PIPELINE_FAILURE',0)}")

    # 5. the human number -- not derivable from this repo, and saying so
    #    is the point. It lives in the Airtable outreach board.
    print(f"5. outreach touches sent/week . NOT TRACKED HERE"
          f"  <- the one that lags; check the Airtable board")

    print()
    print(f"   entry types: {dict(types)}")

    # did the record reach the remote?
    ahead = _git("rev-list", "--count", "@{u}..HEAD")
    dirty = _git("status", "--porcelain")
    if ahead and ahead != "0":
        print(f"   UNPUSHED: {ahead} commit(s) local only — the public record is stale")
    else:
        print("   pushed: local matches origin")
    if dirty:
        print(f"   uncommitted changes in {len(dirty.splitlines())} file(s)")

    # last night's result
    today = datetime.now(timezone.utc).date()
    for back in range(0, 4):
        d = (today - timedelta(days=back)).isoformat()
        got = [e["payload"].get("type") for e in entries
               if e.get("published_at_utc", "")[:10] == d
               and e["payload"].get("type") != "AUDIT_RESULT"]
        flag = "" if got else "  <- NO PIPELINE ENTRY"
        print(f"   {d}: {got or '(nothing)'}{flag}")

    # audit result
    a = AUDIT / f"audit_{today.strftime('%Y%m%d')}.json"
    if a.exists():
        rep = json.loads(a.read_text())
        fails = [c["name"] for c in rep["checks"] if c["status"] in ("FAIL", "ERROR")]
        print(f"   audit today: {rep['overall_status']}"
              + (f" — {', '.join(fails)}" if fails else ""))
    else:
        print("   audit today: has not run yet")

    # unanchored entries (the known anchor_ots tail-only bug)
    orphan = sum(1 for e in entries
                 if (OTS / f"{e['entry_hash']}.hash").exists()
                 and not (OTS / f"{e['entry_hash']}.hash.ots").exists())
    if orphan:
        print(f"   {orphan} entry(s) with .hash but no .ots proof")

    if DB.exists():
        try:
            c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
            n = c.execute("SELECT COUNT(*) FROM corpus").fetchone()[0]
            print(f"   corpus rows: {n}")
            c.close()
        except Exception:
            pass

    print()
    print("   verify: python3 nightshift/verify_chain.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
