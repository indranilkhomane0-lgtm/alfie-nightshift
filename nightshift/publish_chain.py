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


def canonical(obj) -> bytes:
    """Deterministic JSON serialization — key order and separators fixed."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":")).encode()


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
        payload = {
            "type": "PIPELINE_FAILURE",
            "note": "Nightly run did not complete. Published for record continuity.",
            "cycle_date": datetime.now(timezone.utc).strftime("%Y%m%d"),
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
