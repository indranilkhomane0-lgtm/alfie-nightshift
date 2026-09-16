#!/usr/bin/env python3
"""
Alfie Night Shift — tamper-evident record publisher.

Appends each nightly brief to reports/chain.jsonl as a hash-chained entry:
    entry_hash = sha256(prev_hash + canonical_json(payload))

Any edit to any historical entry breaks every hash after it.
Verification requires nothing but Python stdlib (see verify_chain.py).

Every entry also gets a best-effort OpenTimestamps stamp attempt at the
moment it's written (see _stamp_at_publish_time()) -- a calendar-server
miss here is never fatal and never blocks the entry; it defers to
nightshift/anchor_ots.py's backlog sweep, which retries across as many
subsequent runs as it takes. verify_chain.py reports receipt coverage;
it does not gate publishing on it.

Usage (called by the nightly pipeline as its final step):
    python3 nightshift/publish_chain.py --brief nightshift/briefs/brief_YYYYMMDD.txt

Honesty rules enforced here, not by discipline:
  - Losses are published identically to wins (no filtering hook exists).
  - If the pipeline failed, publish a FAILED entry — gaps are suspicious,
    failures are honest.
"""

import argparse
import functools
import hashlib
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

CHAIN_PATH = Path(__file__).resolve().parent.parent / "reports" / "chain.jsonl"
REPO_ROOT = Path(__file__).resolve().parent.parent
PREDICTIONS_PATH = Path(__file__).resolve().parent.parent / "reports" / "predictions.jsonl"
GENESIS_HASH = "0" * 64

# This script runs two ways: directly (`python3 nightshift/publish_chain.py`,
# invoked by run_and_publish.sh with cwd=repo root -- sys.path[0] becomes
# nightshift/, not REPO_ROOT) and imported (`from nightshift.publish_chain
# import append_entry`, e.g. self_audit.py, which already puts REPO_ROOT on
# sys.path before importing). Inserting REPO_ROOT here makes the
# package-qualified import below resolve either way; a duplicate entry from
# the second case is harmless.
sys.path.insert(0, str(REPO_ROOT))
from nightshift.strategy_version import compute_strategy_version  # noqa: E402
from nightshift import anchor_ots as _anchor_ots  # noqa: E402

OTS_DIR = REPO_ROOT / "reports" / "ots"

# Written by run_nightshift.py's cmd_full() at the instant the cycle
# actually raises; read here moments later in the same run_and_publish.sh
# invocation. See run_nightshift.py for why this is a file handoff rather
# than an import into nightshift.cycle (frozen strategy_version surface).
FAILURE_SIDECAR_PATH = REPO_ROOT / "nightshift" / "logs" / "last_pipeline_failure.json"


def _read_and_clear_failure_sidecar() -> dict:
    """failure_class/exception_type for a PIPELINE_FAILURE payload --
    "unknown"/"unknown" if the sidecar is missing, unparseable, or not
    from today (UTC). Never raises, never blocks the entry from being
    written: a --failed entry must always get chained even if this
    classification step can't say anything useful. The sidecar is always
    deleted before returning, success or not, so a stale file can never
    leak its category into a later, unrelated night's entry."""
    info = {"failure_class": "unknown", "exception_type": "unknown"}
    try:
        raw = json.loads(FAILURE_SIDECAR_PATH.read_text())
        written = datetime.fromisoformat(raw["written_at_utc"])
        if written.date() == datetime.now(timezone.utc).date():
            info["failure_class"] = raw.get("failure_class", "unknown")
            info["exception_type"] = raw.get("exception_type", "unknown")
    except Exception:
        pass
    finally:
        FAILURE_SIDECAR_PATH.unlink(missing_ok=True)
    return info


def canonical(obj) -> bytes:
    """Deterministic JSON serialization — key order and separators fixed."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":")).encode()


def _stamp_at_publish_time(entry_hash: str) -> bool:
    """Best-effort, single-attempt OpenTimestamps stamp for the entry just
    written -- reuses nightshift/anchor_ots.py's own find_ots()/stamp()
    rather than duplicating the ots-CLI-invocation logic.

    Deliberately not a retry loop: one attempt, same per-call timeout
    anchor_ots.stamp() already enforces (STAMP_TIMEOUT_S). If the calendar
    is unreachable, `ots` isn't installed, or anything else goes wrong,
    this defers silently -- the chain entry is written either way, just
    without a receipt yet. Nothing here blocks or fails the publish.

    That deferred entry doesn't wait on anything special to get swept up:
    anchor_ots.py's own unanchored_entries() re-scans the WHOLE chain for
    hashes missing a .hash.ots on every single run (moments later in the
    same run_and_publish.sh invocation, and again every subsequent night),
    so a publish-time miss here is retried indefinitely until it succeeds
    -- the same defer-and-sweep shape as push_or_defer() in
    run_and_publish.sh, just architected as "retry via the next sweep"
    rather than "retry N times in this call," because anchor_ots.py's
    sweep already retries across as many nights as it takes.

    Never raises. Return value is informational only -- callers don't
    need to act on it, since a False here is exactly what the backlog
    sweep exists to fix."""
    try:
        ots_bin = _anchor_ots.find_ots()
        if ots_bin is None:
            return False
        OTS_DIR.mkdir(parents=True, exist_ok=True)
        hash_file = OTS_DIR / f"{entry_hash}.hash"
        hash_file.write_text(entry_hash)
        if _anchor_ots.stamp(ots_bin, hash_file):
            return True
        # Never leave a .hash without a .ots -- see anchor_ots.py's own
        # comment on this exact invariant (it's what self_audit's
        # orphan_hash_no_ots check flags). unanchored_entries() only
        # re-attempts entries with NEITHER file present.
        hash_file.unlink(missing_ok=True)
        return False
    except Exception:
        return False


def last_hash() -> str:
    if not CHAIN_PATH.exists() or CHAIN_PATH.stat().st_size == 0:
        return GENESIS_HASH
    with CHAIN_PATH.open("rb") as f:
        last_line = f.read().splitlines()[-1]
    return json.loads(last_line)["entry_hash"]


@functools.lru_cache(maxsize=1)
def _code_version():
    """(git HEAD sha, working-tree-dirty) for the code that wrote this entry.
    Cached per-process — the answer can't change mid-run. Never raises: if
    git is unavailable or the calls fail, both fields become "unknown" so
    the entry still gets written rather than claiming a fact we don't have."""
    try:
        sha = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=REPO_ROOT,
            capture_output=True, text=True, timeout=5, check=True,
        ).stdout.strip()
        dirty = bool(subprocess.run(
            ["git", "status", "--porcelain"], cwd=REPO_ROOT,
            capture_output=True, text=True, timeout=5, check=True,
        ).stdout.strip())
        return sha, dirty
    except Exception:
        return "unknown", "unknown"


@functools.lru_cache(maxsize=1)
def _strategy_version():
    """Content hash over the call-determining surface (nightshift/
    strategy_version.py) -- what actually changed to produce tonight's
    signal, as opposed to code_version's git HEAD sha, which moves on
    every commit including ones that touch none of it. Cached per-process
    like _code_version(): none of the surface files are rewritten mid-run.

    Never raises: a missing/unreadable surface file (corrupted checkout,
    file moved) must not block the entry from being written -- same
    fail-safe philosophy as _code_version(). "unknown" here is the same
    honest placeholder as _code_version()'s "unknown", not a fabricated
    hash and not an omission."""
    try:
        return compute_strategy_version()
    except Exception:
        return "unknown"


def _predictions_snapshot():
    """(row count, sha256, read_error) for reports/predictions.jsonl at
    the moment this entry is chained -- makes deletion or truncation of
    the predictions file detectable against the chain instead of
    silently unnoticed (the gap disclosed in chain entry 70). Same
    fail-safe philosophy as _code_version(): never raises. Missing or
    unreadable file -> (None, None, "<reason>"); an entry must still get
    written, never blocked by this.

    Deliberately NOT cached, unlike _code_version() -- void_predictions.py
    rewrites predictions.jsonl and then calls append_entry() in the same
    process; a cached snapshot from before that rewrite would silently
    chain a stale hash for the very VOID_DECISION entry meant to cover
    it."""
    try:
        data = PREDICTIONS_PATH.read_bytes()
    except FileNotFoundError:
        return None, None, "predictions.jsonl does not exist yet"
    except Exception as exc:
        return None, None, f"could not read predictions.jsonl: {exc!r}"
    n = sum(1 for line in data.splitlines() if line.strip())
    return n, hashlib.sha256(data).hexdigest(), None


def append_entry(payload: dict) -> dict:
    prev = last_hash()
    sha, dirty = _code_version()
    n, preds_sha256, read_error = _predictions_snapshot()
    payload = {
        **payload,
        "code_version": sha, "code_dirty": dirty,
        "strategy_version": _strategy_version(),
        "predictions_n": n, "predictions_sha256": preds_sha256,
    }
    if read_error:
        payload["predictions_read_error"] = read_error
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
    # At publish time, not as an afterthought: best-effort, deferred to
    # anchor_ots.py's backlog sweep on any failure -- see
    # _stamp_at_publish_time()'s docstring. Only after the chain write
    # above, and never allowed to affect it: an entry that exists but
    # isn't yet stamped is normal and expected; an entry that's stamped
    # but was never chained would be backwards.
    _stamp_at_publish_time(entry["entry_hash"])
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
    ap.add_argument(
        "--methodology",
        metavar="DESCRIPTION",
        help="Publish a METHODOLOGY_CHANGE entry: a short description of a "
             "deliberate change to how the system produces or grades its "
             "record, so the change is part of the record instead of an "
             "unexplained discontinuity in it",
    )
    ap.add_argument(
        "--data",
        metavar="JSON",
        help="Optional structured data for a --methodology entry, as a JSON "
             "object string (e.g. '{\"criteria\": {\"min_calls_per_month\": 4, "
             "\"sustained_days\": 90, \"graduation_n\": 30}}'). Stored under "
             "payload[\"data\"] and hashed with everything else. Only valid "
             "alongside --methodology.",
    )
    args = ap.parse_args()

    if args.data and not args.methodology:
        print("error: --data is only valid alongside --methodology", file=sys.stderr)
        return 2

    if args.methodology:
        payload = {
            "type": "METHODOLOGY_CHANGE",
            "description": args.methodology,
            "changed_at_utc": datetime.now(timezone.utc).isoformat(),
        }
        if args.data:
            try:
                payload["data"] = json.loads(args.data)
            except json.JSONDecodeError as exc:
                print(f"error: --data is not valid JSON: {exc}", file=sys.stderr)
                return 2
    elif args.failed:
        failure_info = _read_and_clear_failure_sidecar()
        payload = {
            "type": "PIPELINE_FAILURE",
            "note": "Nightly run did not complete. Published for record continuity.",
            "cycle_date": datetime.now(timezone.utc).strftime("%Y%m%d"),
            "failure_class": failure_info["failure_class"],
            "exception_type": failure_info["exception_type"],
        }
    elif args.brief:
        text = Path(args.brief).read_text()
        payload = {
            "type": "NIGHTLY_BRIEF",
            "brief_file": Path(args.brief).name,
            "brief_sha256": hashlib.sha256(text.encode()).hexdigest(),
        }
        # data-completeness sidecar written by cycle.py next to the brief:
        # which data sources were real tonight vs. fell back to a default.
        # Older briefs (before this existed) simply have no sidecar and the
        # field is omitted rather than guessed.
        completeness_path = Path(args.brief).with_suffix(".completeness.json")
        if completeness_path.exists():
            payload["data_completeness"] = json.loads(completeness_path.read_text())
    else:
        if not args.report:
            print("error: --report, --brief, --failed, or --methodology required",
                  file=sys.stderr)
            return 2
        payload = json.loads(Path(args.report).read_text())
        payload["type"] = payload.get("type", "NIGHTLY_BRIEF")

    entry = append_entry(payload)
    print(f"chained: {entry['entry_hash'][:16]}…  (prev {entry['prev_hash'][:16]}…)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
