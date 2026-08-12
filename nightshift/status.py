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
WATCHDOG_LOG = ROOT / "nightshift" / "logs" / "watchdog.log"

# Pre-registered product criteria. Prefer the most recent chain entry
# carrying payload.data.criteria (published via
# publish_chain.py --methodology "..." --data '{"criteria": {...}}');
# the three constants below are the fallback, used only if no such entry
# exists yet, or per-key if a found entry doesn't carry all three.
# PRE_REGISTRATION_ENTRY_HASH (2026-08-11, "PRE-REGISTRATION OF PRODUCT
# AND DESIGN CRITERIA") pre-registered these as free prose in its
# description, not structured data, and the chain is append-only -- that
# entry can't be edited to add the structured field retroactively -- so
# this fallback stays live until the criteria are re-published under a
# new entry.
PRE_REGISTRATION_ENTRY_HASH = "3c2ee64d54852366c9149c01a4eaa11f20c54fe8a63d455aaa4510b535e194bd"
CRITERIA_FALLBACK_MIN_CALLS_PER_MONTH = 4
CRITERIA_FALLBACK_SUSTAINED_DAYS = 90
CRITERIA_FALLBACK_GRADUATION_N = 30


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
    # Count on the status field, not on "has no outcome yet". VOID
    # predictions have no outcome and never will -- treating them as open
    # overstated the live pipeline 3x (54 reported vs 17 real, 2026-08-02).
    outcomes = Counter(p.get("outcome") or p.get("status") or "OPEN" for p in preds)
    settled_dates = {p.get("settle_date") for p in preds
                     if p.get("outcome") in ("WIN", "LOSS")}
    labeled_rows = outcomes.get("WIN", 0) + outcomes.get("LOSS", 0)
    # Multiple configs on the same asset, same direction, same night grade
    # identically -- same entry price, same settle price, same WIN/LOSS --
    # so they're one unit of evidence, not len(configs). settled_dates
    # above already collapses a whole night to one date regardless of
    # asset; this keeps different assets distinct while still collapsing
    # same-asset repeats, so it can undercount less than settled_dates
    # while still not treating N configs agreeing as N independent votes.
    distinct_calls = {(p.get("asset"), p.get("direction"), p.get("cycle_date"))
                       for p in preds if p.get("outcome") in ("WIN", "LOSS")}
    # Only long/short predictions can ever become WIN or LOSS. A 'none'
    # prediction grades NO_CALL and is excluded from the corpus, so it moves
    # metric 2 not at all. This is the real pipeline toward graduation.
    gradeable = [p for p in preds
                 if p.get("status") == "OPEN"
                 and p.get("direction") in ("long", "short")]
    open_n = sum(1 for p in preds if p.get("status") == "OPEN")
    void_n = sum(1 for p in preds if p.get("status") == "VOID")

    # 1. chain length
    print(f"1. chain length ............... {len(entries)} entries")

    # 2. labeled outcome rows -> meta-model graduation
    print(f"2. labeled settle dates ....... {len(settled_dates)} of 30"
          f"   (WIN {outcomes.get('WIN',0)} / LOSS {outcomes.get('LOSS',0)}"
          f" / NO_CALL {outcomes.get('NO_CALL',0)})")
    print(f"   labeled rows ................ {labeled_rows}"
          f"   distinct (asset,direction,night) calls . {len(distinct_calls)}")
    print(f"   gradeable in flight ........ {len(gradeable)}"
          f"   <- only long/short can ever become WIN/LOSS")
    print(f"   open {open_n} ({open_n - len(gradeable)} are direction=none -> NO_CALL)"
          f", void {void_n}")
    if gradeable:
        nxt = min(gradeable, key=lambda p: p.get("settle_date", "9999"))
        print(f"   next to grade .............. {nxt['prediction_id']}"
              f"  settles {nxt['settle_date']} (graded the day after)")

    # 3. regimes survived with losses published
    print(f"3. losses published ........... {outcomes.get('LOSS',0)}")

    # 4. pipeline failures honestly published
    print(f"4. pipeline failures published  {types.get('PIPELINE_FAILURE',0)}")

    # 5. the human number -- not derivable from this repo, and saying so
    #    is the point. It lives in the Airtable outreach board.
    print(f"5. outreach touches sent/week . NOT TRACKED HERE"
          f"  <- the one that lags; check the Airtable board")

    # Structured criteria override: most recent METHODOLOGY_CHANGE entry
    # carrying payload.data.criteria wins, walked newest-first so the
    # first match is the most recent. Resolution is per-key, not
    # all-or-nothing -- a found entry missing one of the three fields
    # falls back to the hardcoded default for THAT field only, and
    # criteria_note says so explicitly rather than implying full
    # chain-sourcing when it isn't. A note that claims a chain source
    # while some values are still hardcoded is exactly the kind of drift
    # this override exists to remove, so it must never happen silently.
    criteria_entry, criteria_data = None, None
    for e in reversed(entries):
        d = e.get("payload", {}).get("data")
        if isinstance(d, dict) and d.get("criteria"):
            criteria_entry, criteria_data = e, d["criteria"]
            break

    criteria_defaults = {
        "min_calls_per_month": CRITERIA_FALLBACK_MIN_CALLS_PER_MONTH,
        "sustained_days": CRITERIA_FALLBACK_SUSTAINED_DAYS,
        "graduation_n": CRITERIA_FALLBACK_GRADUATION_N,
    }
    if criteria_entry is not None:
        from_chain, from_fallback, resolved = [], [], {}
        for key, default in criteria_defaults.items():
            if key in criteria_data:
                resolved[key] = criteria_data[key]
                from_chain.append(key)
            else:
                resolved[key] = default
                from_fallback.append(key)
        min_calls_per_month = resolved["min_calls_per_month"]
        sustained_days = resolved["sustained_days"]
        graduation_n = resolved["graduation_n"]
        entry_ref = (f"{criteria_entry.get('entry_hash','?')[:8]}… "
                     f"({criteria_entry.get('published_at_utc','?')[:10]})")
        if from_fallback:
            criteria_note = (f"criteria source: PARTIAL -- chain entry {entry_ref} "
                              f"supplies {', '.join(from_chain)}; "
                              f"{', '.join(from_fallback)} still hardcoded fallback")
        else:
            criteria_note = f"criteria source: chain entry {entry_ref} (all three fields)"
    else:
        min_calls_per_month = CRITERIA_FALLBACK_MIN_CALLS_PER_MONTH
        sustained_days = CRITERIA_FALLBACK_SUSTAINED_DAYS
        graduation_n = CRITERIA_FALLBACK_GRADUATION_N
        criteria_note = (f"criteria source: HARDCODED FALLBACK -- no chain entry "
                          f"carries payload.data.criteria yet (pre-registration "
                          f"entry {PRE_REGISTRATION_ENTRY_HASH[:8]}… predates "
                          f"structured data and can't be edited; re-publish under "
                          f"--data to override this fallback)")

    print()
    print(f"── pre-registered criteria ──")
    print(f"   {criteria_note}")
    cutoff_90d = (datetime.now(timezone.utc).date() - timedelta(days=sustained_days)).isoformat()
    # Frequency measures how often the engine PRODUCES a call, not how many
    # produced usable evidence -- a call later voided for bad exchange data
    # was still a call made, so VOID rows count here (unlike distinct_calls
    # above, which is scoped to graded WIN/LOSS rows for the graduation gate).
    recent_distinct_calls = {(p.get("asset"), p.get("direction"), p.get("cycle_date"))
                              for p in preds
                              if p.get("direction") in ("long", "short")
                              and (p.get("cycle_date") or "") >= cutoff_90d}
    recent_voided_calls = {(p.get("asset"), p.get("direction"), p.get("cycle_date"))
                            for p in preds
                            if p.get("direction") in ("long", "short")
                            and p.get("status") == "VOID"
                            and (p.get("cycle_date") or "") >= cutoff_90d}
    calls_per_month = len(recent_distinct_calls) / sustained_days * 30
    print(f"   graduation gate ............. {len(distinct_calls)} of {graduation_n}"
          f" distinct labeled calls (criteria two and three need this)")
    print(f"   one — frequency .............. threshold >={min_calls_per_month}/month"
          f" sustained {sustained_days}d"
          f"   actual {len(recent_distinct_calls)} calls in trailing {sustained_days}d"
          f" = {calls_per_month:.2f}/month"
          f"  ({len(recent_voided_calls)} of those later voided)")
    print(f"   two — edge ................... mean 7d return vs buy-and-hold"
          f"   not yet computable — needs {graduation_n}, have {len(distinct_calls)}")
    print(f"   three — meta-model ........... ranking vs random on held-out rows"
          f"   not yet computable — needs {graduation_n}, have {len(distinct_calls)}")

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

    # watchdog: how often has the unpushed-commit / missing-entry guard
    # actually fired in the last 14 days. SELF-HEALED counts too -- a
    # recurring self-heal is still a recurring push failure, just one
    # that stopped needing a human to notice. Loosely UTC-vs-IST scoped
    # (watchdog.log timestamps are IST, `today` here is UTC) -- fine for
    # a 14-day trend count, not meant to be a precise audit.
    if WATCHDOG_LOG.exists():
        cutoff = (today - timedelta(days=14)).isoformat()
        hits = sum(1 for line in WATCHDOG_LOG.read_text().splitlines()
                   if line[:10] >= cutoff and ("ALERT" in line or "SELF-HEALED" in line))
        print(f"   watchdog: {hits} ALERT/SELF-HEALED in last 14 days")
    else:
        print("   watchdog: no log yet")

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
    print("   verify: python3 core/verify_chain.py")
    print("           (nightshift/verify_chain.py still works -- forwards here)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
