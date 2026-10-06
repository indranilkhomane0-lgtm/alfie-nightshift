#!/usr/bin/env python3
"""
Alfie Night Shift — corpus-delta archive.

corpus.db is gitignored and local-only (see .gitignore: "local backups &
regenerating artifacts -- never commit"). That's correct for corpus.db
itself -- it's a derived SQLite index, rebuildable from this archive --
but it means the meta-model's entire training set has never existed
anywhere but one laptop. This closes that: every night, after the cycle
completes, this script reads back every row nightshift/cycle.py's
insert_config_entry() wrote to corpus.db's `corpus` table for tonight's
cycle_id -- every config that passed all 5 MC gates, not just the one
that got published -- and appends it to reports/archive/corpus_delta.jsonl,
an ever-growing, append-only file in the same spirit as
reports/predictions.jsonl. nightshift/publish_chain.py hashes the whole
file into every chain entry (corpus_delta_n/corpus_delta_sha256), the
same reference-by-hash pattern predictions.jsonl already uses.

Why a separate script, not a change to cycle.py: cycle.py is on the
frozen strategy surface (nightshift/strategy_version.py's
STRATEGY_VERSION_FILES) -- editing it moves strategy_version regardless
of whether the edit touches signal logic ("glue code is as
signal-determining as the logic it calls," no carve-out). Everything
this script needs is already written to corpus.db by code that already
exists; reading it back from the outside, after the fact, touches
nothing on the frozen surface. See the module docstring of
nightshift/stamp_prediction.py's _context_hash() for the other half of
this story (raw OHLCV archiving) -- that one DOES require a frozen-file
change and is deliberately not attempted here.

Scope, deliberately: this archives each corpus row as it existed at
INSERT time (the night the candidate was evaluated). A row can later be
mutated in place by nightshift/db.py's update_live_outcome() (the
live_sharpe_20d/decay_ratio/survived/outcome_date/outcome_note columns,
filled in once a deployed config's live performance is graded, weeks
later) -- those later mutations are NOT re-archived here. A reader
replaying this file sees the corpus exactly as the meta-model would
have seen it for training purposes at ranking time that night, not
its eventual outcome. Closing that gap would need its own append-only
trail of outcome-update events, not a change to this one.

Per-cycle_id idempotent and self-healing, same "defer and sweep" shape
as nightshift/anchor_ots.py and run_and_publish.sh's push_or_defer():
every run computes which COMPLETE cycles (nightshift/db.py's `cycles`
table, status='complete') have no cycle_marker line yet in the archive,
oldest first, and backfills all of them in one pass -- so a disk-full
or permission failure on one night is caught up automatically the next
time this runs, without losing anything or double-writing. A cycle
that never completed (crashed mid-run, status stuck at 'running' or
'error') is skipped on purpose: there is nothing in corpus.db for it to
archive, and that night's failure is already honestly recorded as its
own PIPELINE_FAILURE chain entry -- inventing a marker here would be a
second, redundant place for the same fact to go stale.

A completed cycle with zero mc_passed candidates (every config failed
MC gating that night) still gets a cycle_marker line, with zero
candidate lines following it. Silence would be ambiguous between "not
yet archived" and "genuinely nothing passed" -- this check exists
specifically so those two states are never confused with each other,
same reasoning PIPELINE_FAILURE entries exist for at the chain level
("a missing night is worse than a bad one").

Like self_audit.py and anchor_ots.py: every failure mode here (DB
locked, disk full, permission denied, a corrupt existing archive file)
is caught and logged, never raised. This script always exits 0 -- the
nightly pipeline must never be blocked by it.

Usage (called by run_and_publish.sh after run_nightshift.py succeeds,
before nightshift/publish_chain.py --brief):
    python3 nightshift/archive_corpus.py
"""
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from nightshift.db import get_conn  # noqa: E402

ARCHIVE_PATH = ROOT / "reports" / "archive" / "corpus_delta.jsonl"
LOG_PATH = ROOT / "nightshift" / "logs" / "archive_corpus.log"
IST = timezone(timedelta(hours=5, minutes=30))

# The first cycle_id this archive covers. No backfill: cycles before this
# feature existed have no entry here, and verify_chain.py/the disclosure
# say so plainly rather than silently starting the count at zero. Set to
# today's date at the moment this shipped -- see the METHODOLOGY_CHANGE
# entry disclosing this feature for the exact value and reasoning.
ARCHIVE_START_CYCLE_ID = 20261007


def log(line: str) -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S IST")
    with LOG_PATH.open("a") as f:
        f.write(f"{stamp}  {line}\n")


def _archived_cycle_ids() -> set:
    """Every cycle_id that already has a cycle_marker line in the
    archive -- read fresh each run rather than cached, so a concurrent
    or prior-run write is always seen. Missing or corrupt file -> empty
    set, never raises (an empty archive just means everything is
    backfilled from ARCHIVE_START_CYCLE_ID forward, same as a fresh
    install)."""
    seen = set()
    if not ARCHIVE_PATH.exists():
        return seen
    try:
        with ARCHIVE_PATH.open() as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if row.get("kind") == "cycle_marker":
                    seen.add(row.get("cycle_id"))
    except Exception as exc:
        log(f"ALERT — could not read existing archive to find covered "
            f"cycle_ids, treating as empty: {exc!r}")
        return set()
    return seen


def _pending_cycles(already_archived: set) -> list[dict]:
    """Every COMPLETE cycle (nightshift/db.py's `cycles` table,
    status='complete') at or after ARCHIVE_START_CYCLE_ID that has no
    cycle_marker yet, oldest first. A cycle stuck at 'running' or
    'error' (crashed) is not returned -- see module docstring."""
    with get_conn() as c:
        rows = c.execute(
            "SELECT cycle_id, cycle_date, n_mc_passed FROM cycles "
            "WHERE status='complete' AND cycle_id >= ? "
            "ORDER BY cycle_id ASC",
            (ARCHIVE_START_CYCLE_ID,),
        ).fetchall()
    return [dict(r) for r in rows if r["cycle_id"] not in already_archived]


def _candidate_rows(cycle_id: int) -> list[dict]:
    """Every corpus row inserted for this cycle_id -- every config that
    passed all 5 MC gates that night, not just the one that got
    published. SELECT * (not a hardcoded column list) so this stays
    correct across nightshift/db.py schema migrations (e.g. void_reason,
    added after the original DDL) without needing to be kept in sync by
    hand. Ordered by id so the same cycle always serializes identically
    regardless of when it's read."""
    with get_conn() as c:
        rows = c.execute(
            "SELECT * FROM corpus WHERE cycle_id=? ORDER BY id ASC",
            (cycle_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def _canonical_line(obj: dict) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))


def main() -> int:
    try:
        ARCHIVE_PATH.parent.mkdir(parents=True, exist_ok=True)
        already = _archived_cycle_ids()
        pending = _pending_cycles(already)

        if not pending:
            log("no completed cycles pending archival")
            return 0

        now = datetime.now(timezone.utc).isoformat()
        new_lines = []
        for cyc in pending:
            cid = cyc["cycle_id"]
            candidates = _candidate_rows(cid)
            new_lines.append(_canonical_line({
                "kind": "cycle_marker", "cycle_id": cid,
                "cycle_date": cyc["cycle_date"],
                "n_mc_passed": cyc["n_mc_passed"],
                "archived_at_utc": now,
            }))
            for row in candidates:
                new_lines.append(_canonical_line({"kind": "candidate", **row}))
            log(f"cycle {cid} ({cyc['cycle_date']}): {len(candidates)} "
                f"candidate row(s) archived")

        # Single append of the whole batch -- same atomicity convention
        # publish_chain.py's append_entry() uses for chain.jsonl: one
        # write() call, append mode, never a partial line left behind by
        # a mid-write crash on any ONE night's batch (a crash can at
        # worst lose or duplicate one run's worth of lines, caught by
        # the idempotent re-scan above on the next run -- it never
        # corrupts a PRIOR night's already-flushed lines).
        with ARCHIVE_PATH.open("a") as f:
            f.write("\n".join(new_lines) + "\n")

        log(f"archived {len(pending)} cycle(s), {len(new_lines) - len(pending)} "
            f"candidate row(s) total")
        return 0
    except Exception as exc:
        # Same philosophy as self_audit.py and anchor_ots.py: archiving
        # must never break the nightly run. Log and move on -- the next
        # run's _pending_cycles() scan picks up whatever didn't make it
        # in, exactly like anchor_ots.py's backlog sweep.
        log(f"ALERT — archive_corpus crashed: {exc!r}")
        return 0


if __name__ == "__main__":
    sys.exit(main())
