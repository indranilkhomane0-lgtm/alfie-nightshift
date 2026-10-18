#!/usr/bin/env python3
"""
tests/run_isolated.py -- the one documented way to run this test suite.

Runs `unittest discover` against tests/, wrapped in a structural guard
against exactly the bug class disclosed on the chain: a test writing to
a real production path under nightshift/, reports/, or core/ instead of
its own tmpdir (nightshift/meta_model.pkl, from tests/test_meta_model.py
not patching MetaModel.MODEL_PATH; nightshift/logs/watchdog.log, from
tests/test_watchdog.py not patching watchdog.LOG_PATH -- four confirmed
instances before this file existed, found by manual audit each time).
Per-test mock.patch isolation is still the primary defense -- this is
the backstop that makes a *missed* patch loud and specific instead of
silent, regardless of which Python API did the writing, and regardless
of whether anyone remembers to look.

Two layers, because neither alone is enough:

  1. PREVENTION: every real file and directory under nightshift/,
     reports/, and core/ is chmod'd read-only (a-w) before the suite
     runs. A write attempt by ANY mechanism -- pathlib, builtins.open,
     sqlite3.connect(), gzip.open(), a subprocess inheriting an open fd
     -- raises PermissionError immediately, inside the offending test,
     with a traceback that names it. This is why a pytest conftest
     autouse fixture (the textbook shape for this kind of guard) isn't
     the right fit here even if pytest were installed (it isn't --
     and, separately, known issue not fixed in this pass: venv/bin/pip
     has a broken shebang, pointing at a now-nonexistent
     ~/Desktop/alfie-nightshift/venv/bin/python3.12 from before this
     repo moved, so pip itself can't currently install anything into
     this venv without that being fixed first): a
     fixture that only monkeypatches Python-level open() calls would
     still miss sqlite3.connect() (nightshift/db.py) and gzip.open()
     (nightshift/archive_ohlcv.py), both exercised by this suite.
     Filesystem permissions sit underneath every one of those APIs at
     once, enforced by the OS rather than by Python instrumentation.

  2. DETECTION (backstop): a sha256 manifest of every file under those
     three trees is taken before and after the run and diffed. This
     catches anything layer 1 didn't -- permissions left over from a
     previous crashed run, a process with elevated privileges, a gap
     in what got chmod'd -- and is the only layer that would have
     caught either real incident on its own: meta_model.pkl and
     watchdog.log are both gitignored, so `git status`/`git diff`
     never saw either pollution happen. A manifest keyed on bytes on
     disk doesn't care what git is tracking.

__pycache__/ is excluded from both layers -- Python regenerating its
own bytecode cache during test imports is expected and harmless, not
the thing this guard exists to catch; without the exclusion, every run
would false-positive on it.

Exit code is non-zero if either the test suite failed OR the guard
caught a write -- "tests passed but polluted production" is a failure
here, not a footnote self_audit.py's own guard-presence check (see
check_test_isolation_guard()) exists to make sure nobody can quietly
go back to running `python3 -m unittest discover` instead.

Usage (replaces `python3 -m unittest discover -s tests`):
    python3 tests/run_isolated.py
"""
import hashlib
import os
import stat
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GUARDED_DIRS = [ROOT / "nightshift", ROOT / "reports", ROOT / "core"]
EXCLUDE_DIR_NAMES = {"__pycache__"}
_WRITE_BITS = stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH


def _iter_guarded_paths():
    """Every real file and directory under the guarded trees, skipping
    __pycache__ entirely (files and the directory itself)."""
    for base in GUARDED_DIRS:
        if not base.exists():
            continue
        for p in base.rglob("*"):
            if any(part in EXCLUDE_DIR_NAMES for part in p.parts):
                continue
            yield p


def _lockdown() -> list[tuple[Path, int]]:
    """Chmod every guarded file AND directory read-only -- directories
    too, so a test can't create a brand-new file inside one (that
    needs write permission on the parent, not the new file). Returns
    (path, original_mode) pairs in the exact order chmod'd, so
    _restore() can reverse them precisely."""
    original: list[tuple[Path, int]] = []
    targets = list(_iter_guarded_paths()) + [d for d in GUARDED_DIRS if d.exists()]
    for p in targets:
        try:
            mode = p.stat().st_mode
        except FileNotFoundError:
            continue
        original.append((p, mode))
        try:
            os.chmod(p, mode & ~_WRITE_BITS)
        except FileNotFoundError:
            pass
    return original


def _restore(original: list[tuple[Path, int]]) -> None:
    """Reverse of the lockdown order -- directories (added last in
    _lockdown()) get their write bit back FIRST, so nothing is left
    needing a write into a still-locked parent. Best-effort per path:
    one failure must not stop the rest from being restored."""
    for p, mode in reversed(original):
        try:
            os.chmod(p, mode)
        except FileNotFoundError:
            pass


def _manifest() -> dict[str, str | None]:
    m: dict[str, str | None] = {}
    for p in _iter_guarded_paths():
        if p.is_file():
            try:
                m[str(p)] = hashlib.sha256(p.read_bytes()).hexdigest()
            except (FileNotFoundError, PermissionError):
                m[str(p)] = None
    return m


def main() -> int:
    before = _manifest()
    original_modes = _lockdown()
    try:
        result = subprocess.run(
            [sys.executable, "-m", "unittest", "discover",
             "-s", str(ROOT / "tests"), "-p", "test_*.py"],
            cwd=str(ROOT),
        )
        tests_rc = result.returncode
    finally:
        _restore(original_modes)

    after = _manifest()
    changed = sorted(p for p in (set(before) | set(after)) if before.get(p) != after.get(p))

    if changed:
        print()
        print("=" * 70)
        print("GUARD FAILURE: the test suite wrote to a real production path.")
        print("=" * 70)
        for path in changed:
            status = ("new file" if path not in before else
                       "deleted" if path not in after else "modified")
            print(f"  [{status}] {path}")
        print()
        print("A test wrote outside its own tmpdir. Find it by the path above")
        print("and add the missing mock.patch -- see tests/test_meta_model.py's")
        print("MODEL_PATH patch for the pattern.")
        return 1

    return tests_rc


if __name__ == "__main__":
    sys.exit(main())
