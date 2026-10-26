#!/usr/bin/env python3
"""
Alfie Night Shift — write-once guard for chained briefs.

A brief named on a NIGHTLY_BRIEF chain entry is the published artifact
that entry's brief_sha256 commits to. It must never be rewritten.

How one was: on 2026-09-26 a second NightShiftCycle.run() was started by
a direct run_nightshift.py invocation outside run_and_publish.sh (so
neither its run lock nor its "already chained tonight" guard applied).
cycle_id comes from date.today(), so it was the same 20260926, and Stage
7's unconditional write_text() replaced brief_20260926.txt -- already
chained as entry 276. Nothing chained the new file; the next night's
`git add nightshift/briefs/` in run_and_publish.sh committed it (3c52ba1).

Two layers, both at the write path:
  1. nightshift/cycle.py calls assert_not_chained() before a cycle does
     anything (no DB write, no stamp, no brief), and write_brief() for the
     brief itself, which re-checks immediately before writing.
  2. nightshift/publish_chain.py --brief calls mark_read_only() on the
     file right after chaining it, so any other writer gets
     PermissionError instead of silently replacing it.
nightshift/self_audit.py check 12 (published_ranking_order) re-hashes
chained briefs against brief_sha256 as the after-the-fact backstop.

Reads the chain directly with the stdlib -- no import of publish_chain,
so nightshift/cycle.py (frozen surface) gains one small dependency, not
the publisher.
"""
import json
import os
import stat
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CHAIN_PATH = ROOT / "reports" / "chain.jsonl"

_WRITE_BITS = stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH


class BriefAlreadyChained(RuntimeError):
    """Refusal to (re)write a brief the chain already commits to."""


def chained_brief_files(chain_path: Path | None = None) -> set[str]:
    path = chain_path or CHAIN_PATH
    if not path.exists():
        return set()
    names = set()
    with path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            p = json.loads(line).get("payload", {})
            if p.get("type") == "NIGHTLY_BRIEF" and p.get("brief_file"):
                names.add(p["brief_file"])
    return names


def assert_not_chained(brief_path) -> None:
    name = Path(brief_path).name
    if name in chained_brief_files():
        raise BriefAlreadyChained(
            f"{name} is already on the chain (NIGHTLY_BRIEF); refusing to "
            f"run a cycle that would rewrite it. The published brief for "
            f"this cycle_id is final -- see nightshift/brief_guard.py.")


def write_brief(brief_path, text: str) -> None:
    assert_not_chained(brief_path)
    Path(brief_path).write_text(text, encoding="utf-8")


def mark_read_only(path) -> None:
    p = Path(path)
    os.chmod(p, p.stat().st_mode & ~_WRITE_BITS)
