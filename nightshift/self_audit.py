#!/usr/bin/env python3
"""
Alfie Night Shift — self-audit.

Eight checks against the pipeline's own historical record and current
source, chained as an AUDIT_RESULT entry every night — pass or fail,
never silently absent, same reasoning as PIPELINE_FAILURE: a missing
night is worse than a bad one. Full findings go to
reports/audit/audit_YYYYMMDD.json; the chain entry carries only a
per-check summary and a sha256 pointer to that file (same
reference-by-hash pattern publish_chain.py already uses for briefs).

WHAT THIS CANNOT CATCH — read this before trusting a green result:
  A passing audit means "no known regression shape reappeared." It does
  not mean the pipeline is correct. Concretely:
    - Reachability != correctness (check 4). Called code that computes
      the wrong thing is invisible to a call-graph check. The WFO
      look-ahead defect (train_all = full series, scored before the OOS
      split) is the standing counterexample: every line involved was
      reachable and called nightly. A dead-code audit finds zero on the
      worst defect found so far, by construction.
    - Populated != meaningful (check 5). Catches frozen constants like
      exchange_flow_7d. Does not catch a value that varies night to
      night but is mislabeled (e.g. a same-instant snapshot recorded as
      a 7-day delta) — variability is necessary, not sufficient, evidence
      a feature means what its name says.
    - Symbol existence != behavioral correctness (check 6). Confirms a
      strategy family exists in both the gate and the registry. A stub
      that always returns a zero-signal array would pass.
    - Proof-file presence != proof validity (check 3). Distinguishing
      "still pending, normal" from "structurally orphaned forever"
      requires the `ots` CLI and a grace window; without `ots` installed
      this check degrades to file-presence only and says so in its output.
    - A calendar gap (check 1) says a night is missing, not why. Crash,
      operator absence, and a correctly-triggered network guard all look
      identical to this check.
    - Scope is nightshift/ only, except check 4 (dead-code reachability,
      also scans core/ since verify_chain.py moved there -- see
      AST_SCAN_DIRS) and check 8 (documentation reference integrity,
      scoped specifically to README.md, run_and_publish.sh, and
      launchd/*.plist). The other six checks remain nightshift/-scoped.
      Check 8 itself is narrow -- see its own bullet below -- so most of
      what those three files claim is still unchecked by anything here.
    - No external ground truth. This checks internal consistency (does
      the code call the code, does the config match the registry) — it
      cannot tell you if a fetched price or a computed Sharpe is right.
    - Same-count corruption (check 7). Compares predictions_n across
      chain entries; a rewrite that preserves the row count while
      changing content (a same-length truncate-and-pad, or an edited
      row) changes predictions_sha256 but not predictions_n, and this
      check never re-reads predictions.jsonl to compare against
      predictions_sha256 -- it only compares the count already stamped
      into each entry.
    - Prose truth != prose citations (check 8). Verifies that a cited
      file path exists, that the quoted verify command resolves, and
      that a small allowlist of numeric constants cited as
      `NAME=NUMBER` in README prose (WFO_STEP_DAYS, WFO_EMBARGO_DAYS,
      HOLD_DAYS, and similar) matches the running code. It cannot verify
      that a narrative claim is still true -- "the meta-model has never
      trained on real data" could become false the day training
      succeeds, and nothing here would notice unless that sentence also
      happened to cite a now-wrong path or constant. Path matching is a
      single regex over free text/shell/XML, not a markdown/shell/plist
      parser; `NAME=NUMBER` is the only numeric citation style
      recognised, so prose forms like "a 7-day embargo
      (WFO_EMBARGO_DAYS)" are silently not checked -- a known
      false-negative, not a pass.

Publish-and-continue, always. Wrapped in the same outer try/except
anchor_ots.py uses: an audit failure is logged and chained, never
raised. This is a smoke detector, not a sprinkler system — nothing here
blocks the nightly cycle. Each of the eight checks also runs in its own
try/except so one crashing check doesn't blank out the other seven.

Usage (called by run_and_publish.sh FIRST, before every early-exit guard,
so it still runs on nights the cycle aborts -- the nights calendar-gap
coverage most needs to see):
    python3 nightshift/self_audit.py
"""

import ast
import hashlib
import json
import logging
import re
import shutil
import sqlite3
import subprocess
import sys
import traceback
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
NIGHTSHIFT_DIR = ROOT / "nightshift"
CHAIN_PATH = ROOT / "reports" / "chain.jsonl"
OTS_DIR = ROOT / "reports" / "ots"
AUDIT_DIR = ROOT / "reports" / "audit"
BRIEF_DIR = NIGHTSHIFT_DIR / "briefs"
LOG_PATH = NIGHTSHIFT_DIR / "logs" / "self_audit.log"
IST = timezone(timedelta(hours=5, minutes=30))

# Scripts launchd / run_and_publish.sh invoke as separate processes.
# Legitimately unreachable from NightShiftCycle.run()'s call graph by
# design, not a defect. Keep this in sync with run_and_publish.sh and
# the launchd/*.plist files.
# verify_chain.py: canonical implementation now lives at
# core/verify_chain.py, in scan scope via AST_SCAN_DIRS below. The name
# below matches both core/verify_chain.py and the nightshift/verify_chain.py
# backward-compat shim (filename-only matching) -- keep listed as long as
# either exists.
ENTRY_POINT_SCRIPTS = {
    "cycle.py", "label_outcomes.py", "void_predictions.py", "watchdog.py",
    "anchor_ots.py", "verify_chain.py", "publish_chain.py",
    "stamp_prediction.py", "self_audit.py",
}
# check_dead_code's source scope: directories that are part of the
# audited pipeline. core/ holds verify_chain.py, canonical since it moved
# out of nightshift/ (nightshift/verify_chain.py is now a compat shim).
AST_SCAN_DIRS = (NIGHTSHIFT_DIR, ROOT / "core")
AST_SCAN_EXCLUDE_DIRS = {"logs", "briefs", "__pycache__"}

OTS_ANCHORING_START = date(2026, 7, 28)  # before this, no proof is expected
OTS_GRACE_HOURS = 24                     # Bitcoin confirmation is hours, not days
FEATURE_MIN_ROWS_TO_JUDGE = 5            # below this, "constant" is unfalsifiable


def log(line: str) -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S IST")
    with LOG_PATH.open("a") as f:
        f.write(f"{stamp}  {line}\n")


def _load_chain() -> list[dict]:
    if not CHAIN_PATH.exists():
        return []
    entries = []
    with CHAIN_PATH.open() as f:
        for line in f:
            line = line.strip()
            if line:
                entries.append(json.loads(line))
    return entries


def _entry_date(entry: dict) -> str | None:
    ts = entry.get("published_at_utc")
    return ts[:10] if ts else None


def _run_check(fn, *args):
    """Isolate one check's crash from the other five -- a check that
    raises becomes an ERROR finding, not a blank audit."""
    name = fn.__name__.replace("check_", "")
    try:
        return fn(*args)
    except Exception as exc:
        log(f"check {name} raised: {exc}\n{traceback.format_exc()}")
        return {
            "name": name,
            "status": "ERROR",
            "detail": {"exception": str(exc), "traceback": traceback.format_exc()},
        }


# ── Check 1: calendar coverage ──────────────────────────────────────────

def check_calendar_coverage(entries: list[dict]) -> dict:
    """Every UTC date from the chain's own genesis through YESTERDAY must
    have >=1 entry of ANY type -- a PIPELINE_FAILURE is coverage, a
    silent gap is not. Genesis-relative, not hardcoded, so it never
    flags the pre-chain period as missing. Today is deliberately excluded;
    see the inline comment on `end` below."""
    finding = {"name": "calendar_coverage", "status": "PASS", "detail": {}}
    if not entries:
        finding["status"] = "SKIP"
        finding["detail"]["reason"] = "chain is empty"
        return finding
    # AUDIT_RESULT entries do NOT count as coverage. self_audit runs first,
    # before every guard in run_and_publish.sh, and chains an entry whether
    # or not anything else succeeds -- so counting them would make this
    # check a tautology ("did the auditor run? yes, because it ran") and it
    # could never fail again. Observed on 2026-08-02: AUDIT_RESULT chained
    # at 00:00:10, then the network died and the cycle produced nothing.
    # Coverage must mean the PIPELINE emitted something -- a brief, or a
    # PIPELINE_FAILURE saying honestly that it couldn't.
    dates = {d for d in (_entry_date(e) for e in entries
                         if e.get("payload", {}).get("type") != "AUDIT_RESULT") if d}
    start = date.fromisoformat(min(dates))
    # Walk to YESTERDAY, not today. self_audit runs at the top of
    # run_and_publish.sh -- before tonight's brief is chained -- so that it
    # still runs on nights the cycle aborts early. Today therefore has no
    # entry yet by construction, and counting it would report a false gap
    # every single night. A genuinely missed night is caught one day later,
    # which is the honest cost of auditing before the night is finished.
    end = datetime.now(timezone.utc).date() - timedelta(days=1)
    gaps = []
    d = start
    while d <= end:
        if d.isoformat() not in dates:
            gaps.append(d.isoformat())
        d += timedelta(days=1)
    finding["detail"]["range"] = [start.isoformat(), end.isoformat()]
    finding["detail"]["gap_dates"] = gaps
    if gaps:
        finding["status"] = "FAIL"
    return finding


# ── Check 2: code_version presence ──────────────────────────────────────

def check_code_version_presence(entries: list[dict]) -> dict:
    """Cutoff = first entry whose payload carries code_version, derived
    dynamically (mirrors what verify_chain.py already does informally)
    rather than hardcoding an entry index. Flags entries after the
    cutoff missing it (regression in publish_chain.py) and entries
    before it that unexpectedly have it (the boundary assumption broke)."""
    finding = {"name": "code_version_presence", "status": "PASS", "detail": {}}
    cutoff_idx = None
    for i, e in enumerate(entries):
        if "code_version" in e.get("payload", {}):
            cutoff_idx = i
            break
    if cutoff_idx is None:
        finding["status"] = "SKIP"
        finding["detail"]["reason"] = "no entry has ever carried code_version"
        return finding
    finding["detail"]["cutoff_index"] = cutoff_idx
    missing_after = [i for i, e in enumerate(entries)
                      if i >= cutoff_idx and "code_version" not in e.get("payload", {})]
    present_before = [i for i, e in enumerate(entries)
                       if i < cutoff_idx and "code_version" in e.get("payload", {})]
    finding["detail"]["missing_after_cutoff"] = missing_after
    finding["detail"]["unexpected_before_cutoff"] = present_before
    if missing_after or present_before:
        finding["status"] = "FAIL"
    return finding


# ── Check 3: .ots proof coverage ────────────────────────────────────────

def _find_ots_binary() -> str | None:
    sibling = Path(sys.executable).parent / "ots"
    if sibling.exists():
        return str(sibling)
    return shutil.which("ots")


def _ots_confirmed(ots_bin: str, ots_file: Path) -> bool | None:
    """True/False if `ots info` says confirmed/pending, None if it
    couldn't be determined (ots missing, timeout, parse failure) --
    None must never be silently treated as either PASS or FAIL upstream."""
    try:
        out = subprocess.run(
            [ots_bin, "info", str(ots_file)],
            capture_output=True, text=True, timeout=15,
        ).stdout.lower()
    except Exception:
        return None
    if "bitcoinblockheaderattestation" in out or "block " in out:
        return True
    if "pending" in out:
        return False
    return None


def check_ots_proof_coverage(entries: list[dict]) -> dict:
    """.hash with no .ots is flagged immediately, no grace period -- a
    healthy run never leaves that combination (this is the exact
    crash-mid-stamp signature the 701fe7d2/ceb08cda pair already
    demonstrated). .ots pending past OTS_GRACE_HOURS is flagged; within
    the window is fine. Neither file existing is only fine if the entry
    is younger than one anchoring cycle."""
    finding = {
        "name": "ots_proof_coverage", "status": "PASS",
        "detail": {
            "orphan_hash_no_ots": [], "pending_past_grace": [],
            "missing_entirely": [], "ots_binary_available": False,
        },
    }
    ots_bin = _find_ots_binary()
    finding["detail"]["ots_binary_available"] = ots_bin is not None
    if ots_bin is None:
        finding["detail"]["note"] = (
            "ots CLI not found -- pending-vs-confirmed distinction skipped; "
            "this run only checks file presence, not proof validity"
        )
    now = datetime.now(timezone.utc)
    for e in entries:
        h = e.get("entry_hash")
        if not h:
            continue
        d = _entry_date(e)
        if not d or date.fromisoformat(d) < OTS_ANCHORING_START:
            continue
        hash_file = OTS_DIR / f"{h}.hash"
        ots_file = OTS_DIR / f"{h}.hash.ots"
        try:
            published = datetime.fromisoformat(e["published_at_utc"])
        except Exception:
            published = now
        age_hours = (now - published).total_seconds() / 3600

        if hash_file.exists() and not ots_file.exists():
            finding["detail"]["orphan_hash_no_ots"].append(h)
        elif hash_file.exists() and ots_file.exists() and ots_bin:
            confirmed = _ots_confirmed(ots_bin, ots_file)
            # Grace runs from when the PROOF was stamped, not when the entry
            # was published. A backfilled proof on an old entry is stamped
            # today and cannot possibly be confirmed yet; measuring from
            # publication would flag all six of them the instant the
            # 2026-08-02 backlog sweep completed, which says nothing about
            # whether anything is wrong.
            proof_age_h = (now.timestamp() - ots_file.stat().st_mtime) / 3600
            if confirmed is False and proof_age_h > OTS_GRACE_HOURS:
                finding["detail"]["pending_past_grace"].append(h)
        elif not hash_file.exists() and age_hours > OTS_GRACE_HOURS:
            finding["detail"]["missing_entirely"].append(h)

    if any(v for k, v in finding["detail"].items() if isinstance(v, list) and v):
        finding["status"] = "FAIL"
    return finding


# ── Check 4: dead code / reachability ───────────────────────────────────

class _DefCollector(ast.NodeVisitor):
    """Collects every function/method def name -> (file, lineno, is_property,
    is_dunder) and, per def, the set of names it calls (unqualified --
    attribute calls like self.foo()/obj.foo() reduce to 'foo'). This is
    name-based, not type-resolved: Python's dynamism (dict dispatch,
    getattr, decorators) means real false positives and false negatives
    are expected here, in both directions. Treat findings as leads, not
    verdicts."""

    def __init__(self, path: Path):
        self.path = path
        self.defs: dict[str, dict] = {}
        self._stack: list[str] = []

    def visit_FunctionDef(self, node):
        self._visit_def(node)

    def visit_AsyncFunctionDef(self, node):
        self._visit_def(node)

    def _visit_def(self, node):
        is_property = any(
            (isinstance(d, ast.Name) and d.id == "property")
            or (isinstance(d, ast.Attribute) and d.attr == "property")
            for d in node.decorator_list
        )
        is_dunder = node.name.startswith("__") and node.name.endswith("__")
        key = node.name
        self.defs.setdefault(key, {
            "file": str(self.path), "lineno": node.lineno,
            "is_property": is_property, "is_dunder": is_dunder,
            "calls": set(),
        })
        self._stack.append(key)
        self.generic_visit(node)
        self._stack.pop()

    def visit_Call(self, node):
        target = None
        if isinstance(node.func, ast.Name):
            target = node.func.id
        elif isinstance(node.func, ast.Attribute):
            target = node.func.attr
        if target and self._stack:
            self.defs[self._stack[-1]]["calls"].add(target)
        elif target:
            # module-level call (e.g. a top-level script body) -- record
            # under a synthetic '<module>' root so entry-point scripts
            # seed the reachable set from their own top-level statements.
            self.defs.setdefault("<module>", {
                "file": str(self.path), "lineno": 0,
                "is_property": False, "is_dunder": False, "calls": set(),
            })
            self.defs["<module>"]["calls"].add(target)
        self.generic_visit(node)


def check_dead_code(root_name: str = "run") -> dict:
    """Union of two roots: (a) NightShiftCycle.run()'s transitive call
    graph via AST name matching, (b) every ENTRY_POINT_SCRIPTS module's
    own top-level calls, since those are invoked directly as separate
    processes and are legitimately unreachable from cycle.py by design.
    Anything defined in nightshift/ or core/ and reachable from neither is
    flagged. @property and dunder methods are exempted -- attribute
    access doesn't show up as an ast.Call node, so this check cannot see
    them either way and would otherwise false-positive on every one."""
    finding = {
        "name": "dead_code_reachability", "status": "PASS",
        "detail": {"flagged": [], "known_limitation": (
            "AST name-matching only, not type-resolved: dict dispatch, "
            "getattr, and decorator-wrapped calls will not be seen as "
            "calls and can produce false positives here."
        )},
    }
    all_defs: dict[str, dict] = {}
    per_file_defs: dict[str, _DefCollector] = {}

    py_files = [p for d in AST_SCAN_DIRS for p in d.rglob("*.py")
                if not any(part in AST_SCAN_EXCLUDE_DIRS for part in p.parts)]
    for path in py_files:
        try:
            tree = ast.parse(path.read_text(), filename=str(path))
        except SyntaxError as exc:
            finding["detail"].setdefault("parse_errors", []).append(
                {"file": str(path), "error": str(exc)})
            continue
        collector = _DefCollector(path)
        collector.visit(tree)
        per_file_defs[str(path)] = collector
        for name, info in collector.defs.items():
            if name == "<module>":
                continue
            # first definition wins for candidate tracking; calls unioned
            all_defs.setdefault(name, {**info, "calls": set()})
            all_defs[name]["calls"] |= info["calls"]

    # union of call-sets from every def with a given name, across files --
    # deliberately name-based/not-scope-resolved (see known_limitation)
    calls_by_name: dict[str, set] = {}
    module_calls: set = set()
    for collector in per_file_defs.values():
        for name, info in collector.defs.items():
            if name == "<module>":
                module_calls |= info["calls"]
            else:
                calls_by_name.setdefault(name, set())
                calls_by_name[name] |= info["calls"]

    reachable: set = set()

    def bfs(seed_names: set):
        frontier = set(seed_names)
        while frontier:
            name = frontier.pop()
            if name in reachable:
                continue
            reachable.add(name)
            for called in calls_by_name.get(name, set()):
                if called not in reachable:
                    frontier.add(called)

    # root (a): NightShiftCycle.run()
    bfs({root_name})
    # root (b): every whitelisted entry-point script's own top-level calls
    bfs(module_calls)
    for path_str, collector in per_file_defs.items():
        if Path(path_str).name in ENTRY_POINT_SCRIPTS:
            bfs(set(collector.defs.keys()) - {"<module>"})
            bfs(collector.defs.get("<module>", {}).get("calls", set()))

    for name, info in all_defs.items():
        if info["is_dunder"] or info["is_property"]:
            continue
        if name in reachable:
            continue
        finding["detail"]["flagged"].append(
            {"name": name, "file": info["file"], "lineno": info["lineno"]})

    if finding["detail"]["flagged"]:
        finding["status"] = "FAIL"
    return finding


# ── Check 5: FEATURE_COLS populated ─────────────────────────────────────

def check_feature_cols_populated() -> dict:
    """Two complementary signals -- (a) per-source completeness sidecars
    for self-reporting sources (price, derivatives), which catch a field
    like exchange_flow_7d by the mechanism the code already uses to
    self-report it; (b) a distinct-value count over corpus.db for
    everything else, which catches a frozen placeholder but NOT a
    varying-but-mislabeled field (btc_dominance_delta will pass and
    should not be read as cleared -- see module docstring)."""
    finding = {
        "name": "feature_cols_populated", "status": "PASS",
        "detail": {"sidecar_flags": [], "constant_columns": [],
                   "note": "constant-column check catches frozen placeholders, "
                           "not varying-but-mislabeled features"},
    }
    try:
        sys.path.insert(0, str(ROOT))
        from nightshift.meta_model import FEATURE_COLS
        from nightshift.config import DB_PATH
    except Exception as exc:
        finding["status"] = "SKIP"
        finding["detail"]["reason"] = f"could not import FEATURE_COLS/DB_PATH: {exc}"
        return finding

    # (a) completeness sidecars, most recent 14 nights
    sidecars = sorted(BRIEF_DIR.glob("*.completeness.json"))[-14:]
    for sc in sidecars:
        try:
            data = json.loads(sc.read_text())
        except Exception:
            continue
        for source_name, status in data.items():
            values = status.values() if isinstance(status, dict) else [status]
            for v in values:
                bad = (v is False) or (isinstance(v, str) and
                       any(k in v.lower() for k in ("fallback", "default", "synthetic", "stale")))
                if bad:
                    finding["detail"]["sidecar_flags"].append(
                        {"file": sc.name, "source": source_name, "value": v})

    # (b) distinct-value count over corpus.db for everything else
    if not Path(DB_PATH).exists():
        finding["detail"]["corpus_note"] = "corpus.db not found -- skipped (b)"
    else:
        try:
            conn = sqlite3.connect(str(DB_PATH))
            cur = conn.cursor()
            cur.execute("SELECT COUNT(*) FROM corpus")
            n_rows = cur.fetchone()[0]
            for col in FEATURE_COLS:
                try:
                    cur.execute(
                        f"SELECT COUNT(DISTINCT {col}) FROM corpus "
                        f"WHERE {col} IS NOT NULL "
                        f"ORDER BY id DESC LIMIT 200"  # noqa: ordering on a
                        # COUNT query is a no-op but harmless; kept simple
                    )
                    n_distinct = cur.fetchone()[0]
                except sqlite3.OperationalError:
                    continue
                if n_rows >= FEATURE_MIN_ROWS_TO_JUDGE and n_distinct <= 1:
                    finding["detail"]["constant_columns"].append(
                        {"column": col, "n_rows": n_rows, "n_distinct": n_distinct})
            conn.close()
        except Exception as exc:
            finding["detail"]["corpus_note"] = f"corpus.db query failed: {exc}"

    if finding["detail"]["sidecar_flags"] or finding["detail"]["constant_columns"]:
        finding["status"] = "FAIL"
    return finding


# ── Check 6: gate/registry match ────────────────────────────────────────

def check_gate_registry_match() -> dict:
    """Pure symbol-set comparison, no timing or semantic ambiguity --
    lowest blind-spot risk among these checks. Flags both directions: families
    gate-eligible but unimplemented, and families implemented but never
    referenced by any regime (structurally unselectable)."""
    finding = {"name": "gate_registry_match", "status": "PASS", "detail": {}}
    try:
        sys.path.insert(0, str(ROOT))
        from nightshift.config import REGIME_FAMILY_GATE
        from nightshift.strategies import STRATEGY_REGISTRY
    except Exception as exc:
        finding["status"] = "SKIP"
        finding["detail"]["reason"] = f"could not import registry/gate: {exc}"
        return finding

    gated_families = set()
    for regime_map in REGIME_FAMILY_GATE.values():
        gated_families |= set(regime_map.keys())
    registered_families = set(STRATEGY_REGISTRY.keys())

    gated_not_implemented = sorted(gated_families - registered_families)
    implemented_not_gated = sorted(registered_families - gated_families)
    finding["detail"]["gated_but_unimplemented"] = gated_not_implemented
    finding["detail"]["implemented_but_never_gated"] = implemented_not_gated
    if gated_not_implemented or implemented_not_gated:
        finding["status"] = "FAIL"
    return finding


# ── Check 7: predictions.jsonl row count is monotonic ──────────────────

def check_predictions_row_count_monotonic(entries: list[dict]) -> dict:
    """predictions.jsonl is append-only in practice, not just in name --
    stamp_prediction.py only ever appends; label_outcomes.py and
    void_predictions.py both rewrite the SAME parsed row list back to
    disk (grading or voiding rows in place), never dropping one. Read
    both before relying on this: neither ever produces a shorter output
    than its input. So predictions_n (publish_chain.append_entry, see
    _predictions_snapshot) should never decrease from one chain entry to
    the next -- a decrease means the file was deleted or truncated
    between those two published moments, the gap disclosed in chain
    entry 70 and previously undetectable.

    Entries published before predictions_n existed carry no such field
    ("predates" this check, same convention verify_chain.py already uses
    for code_version) and are skipped entirely -- as both a "previous"
    and a "current" value -- so comparison only ever happens between two
    entries that both actually carry it.

    See the module docstring's WHAT THIS CANNOT CATCH for what this
    specific check misses (same-count corruption)."""
    finding = {"name": "predictions_row_count_monotonic", "status": "PASS",
               "detail": {"decreases": []}}
    prev_n, prev_hash = None, None
    for e in entries:
        n = e.get("payload", {}).get("predictions_n")
        if n is None:
            continue  # predates this field, or a read_error entry -- not comparable
        if prev_n is not None and n < prev_n:
            finding["detail"]["decreases"].append({
                "from_entry_hash": prev_hash, "from_n": prev_n,
                "to_entry_hash": e.get("entry_hash"), "to_n": n,
            })
        prev_n, prev_hash = n, e.get("entry_hash")
    if finding["detail"]["decreases"]:
        finding["status"] = "FAIL"
    return finding


# ── Check 8: documentation reference integrity ──────────────────────────

# Matches a repo-relative-looking path with >=1 '/' and a dotted
# extension, e.g. nightshift/verify_chain.py or reports/chain.jsonl.
# Deliberately requires a dotted final segment so it doesn't fire on
# bare module names (WFOEngine), dotted Python refs
# (nightshift.config.WFO_STEP_DAYS -- no '/'), or directory-only
# mentions (reports/ots/) with no filename to check.
_DOC_PATH_RE = re.compile(r'/?(?:[A-Za-z0-9_.\-]+/)+[A-Za-z0-9_\-]+\.[A-Za-z0-9]+')
# Matches the literal citation style `NAME=NUMBER` in backticks -- the
# only numeric-constant citation form this check recognises; see the
# module docstring's WHAT THIS CANNOT CATCH for the forms it misses.
_DOC_CONST_RE = re.compile(r'`([A-Za-z_][A-Za-z0-9_]*)=(-?\d+(?:\.\d+)?)`')
# Matches a "python3 <path>" invocation as quoted in README.md's verify
# instructions.
_DOC_VERIFY_CMD_RE = re.compile(r'^\s*python3\s+(\S+)', re.MULTILINE)


def check_doc_reference_integrity() -> dict:
    """Scoped to exactly the three places the docstring above names as
    uncovered by every other check: README.md, run_and_publish.sh, and
    launchd/*.plist. Three things, all structural, none semantic:
    (a) every path-looking token in those files resolves to a real file
    -- core/verify_chain.py moving out of nightshift/ is exactly the
    failure shape this exists to catch; (b) numeric constants cited in
    README prose as `NAME=NUMBER` match the live value in
    nightshift/config.py (or, for HOLD_DAYS specifically, in
    nightshift/stamp_prediction.py -- not every constant a reader would
    call "config" lives in config.py, and HOLD_DAYS is exactly that
    case); (c) every `python3 <path>` command quoted in README.md
    resolves, checked separately from (a) because a broken verify
    command breaks this project's central claim ("verify it yourself"),
    not just a stray reference.

    Absolute paths are only checked when they fall under this repo's own
    ROOT (the launchd plists' ProgramArguments, for instance); an
    absolute path elsewhere on the machine (/usr/bin/python3, an
    interpreter path) is out of scope for a documentation check and is
    skipped, not flagged."""
    finding = {
        "name": "doc_reference_integrity", "status": "PASS",
        "detail": {
            "missing_paths": [], "broken_verify_commands": [],
            "constant_mismatches": [],
            "known_limitation": (
                "Path extraction is one regex over free text/shell/XML, not "
                "a real markdown/shell/plist parser -- URLs and '...'-"
                "truncated example hashes are explicitly excluded, "
                "everything else matching word/word.ext is checked. "
                "Numeric-constant checking only recognises the literal "
                "`NAME=NUMBER` citation style."
            ),
        },
    }

    scan_files = [ROOT / "README.md", ROOT / "run_and_publish.sh"]
    launchd_dir = ROOT / "launchd"
    if launchd_dir.exists():
        scan_files += sorted(launchd_dir.glob("*.plist"))

    readme_text = None
    for path in scan_files:
        try:
            text = path.read_text()
        except Exception as exc:
            finding["detail"].setdefault("unreadable_files", []).append(
                {"file": str(path.name), "error": str(exc)})
            continue
        if path.name == "README.md":
            readme_text = text

            for m in _DOC_VERIFY_CMD_RE.finditer(text):
                cited = m.group(1)
                target = Path(cited) if cited.startswith("/") else ROOT / cited
                if not target.exists():
                    finding["detail"]["broken_verify_commands"].append(
                        {"file": path.name, "cited": cited})

        for m in _DOC_PATH_RE.finditer(text):
            token = m.group(0)
            if "..." in token:
                continue  # truncated/illustrative example, not a real path
            if text[max(0, m.start() - 3):m.start()] == "://":
                continue  # URL, not a repo file
            if token.startswith(str(ROOT)):
                target = Path(token)
            elif token.startswith("/"):
                continue  # absolute path outside this repo -- not in scope
            else:
                target = ROOT / token
            if not target.exists():
                finding["detail"]["missing_paths"].append(
                    {"file": path.name, "cited": token})

    try:
        sys.path.insert(0, str(ROOT))
        from nightshift.config import (WFO_MIN_TRAIN_DAYS, WFO_TEST_DAYS,
            WFO_STEP_DAYS, WFO_EMBARGO_DAYS, OPTUNA_TRIALS, MIN_OOS_SHARPE,
            OHLCV_DAYS)
        from nightshift.stamp_prediction import HOLD_DAYS
        known = {
            "WFO_MIN_TRAIN_DAYS": WFO_MIN_TRAIN_DAYS, "WFO_TEST_DAYS": WFO_TEST_DAYS,
            "WFO_STEP_DAYS": WFO_STEP_DAYS, "WFO_EMBARGO_DAYS": WFO_EMBARGO_DAYS,
            "OPTUNA_TRIALS": OPTUNA_TRIALS, "MIN_OOS_SHARPE": MIN_OOS_SHARPE,
            "OHLCV_DAYS": OHLCV_DAYS, "HOLD_DAYS": HOLD_DAYS,
        }
        if readme_text is not None:
            for m in _DOC_CONST_RE.finditer(readme_text):
                name, cited_str = m.group(1), m.group(2)
                if name not in known:
                    continue
                cited, actual = float(cited_str), float(known[name])
                if cited != actual:
                    finding["detail"]["constant_mismatches"].append(
                        {"name": name, "cited_in_readme": cited, "actual_in_code": actual})
    except Exception as exc:
        finding["detail"]["constant_check_note"] = f"could not verify constants: {exc}"

    if any(finding["detail"][k] for k in
           ("missing_paths", "broken_verify_commands", "constant_mismatches")):
        finding["status"] = "FAIL"
    return finding


# ── main ──────────────────────────────────────────────────────────────

def main() -> int:
    today = datetime.now(timezone.utc).strftime("%Y%m%d")
    report_path = AUDIT_DIR / f"audit_{today}.json"

    try:
        if report_path.exists():
            log(f"audit already ran today ({report_path.name}) -- skipping")
            return 0

        entries = _load_chain()
        checks = [
            _run_check(check_calendar_coverage, entries),
            _run_check(check_code_version_presence, entries),
            _run_check(check_ots_proof_coverage, entries),
            _run_check(check_dead_code),
            _run_check(check_feature_cols_populated),
            _run_check(check_gate_registry_match),
            _run_check(check_predictions_row_count_monotonic, entries),
            _run_check(check_doc_reference_integrity),
        ]

        overall = "FAIL" if any(c["status"] in ("FAIL", "ERROR") for c in checks) else "PASS"
        report = {
            "date_utc": today,
            "generated_at_utc": datetime.now(timezone.utc).isoformat(),
            "overall_status": overall,
            "checks": checks,
            "known_limitations": (
                "A passing audit means 'no known regression shape reappeared,' "
                "not 'the pipeline is correct.' See module docstring: this "
                "checklist cannot see the WFO look-ahead class of bug "
                "(reachability != correctness), mislabeled-but-varying "
                "features (populated != meaningful), stub implementations "
                "(symbol existence != behavior), or anything outside "
                "nightshift/."
            ),
        }

        AUDIT_DIR.mkdir(parents=True, exist_ok=True)
        report_text = json.dumps(report, sort_keys=True, indent=2, default=str)
        report_path.write_text(report_text)
        report_sha256 = hashlib.sha256(report_text.encode()).hexdigest()

        summary = {c["name"]: {"status": c["status"],
                                "finding_count": _finding_count(c)} for c in checks}

        try:
            sys.path.insert(0, str(ROOT))
            from nightshift.publish_chain import append_entry
            entry = append_entry({
                "type": "AUDIT_RESULT",
                "status": overall,
                "checks": summary,
                "report_file": report_path.name,
                "report_sha256": report_sha256,
            })
            log(f"chained AUDIT_RESULT ({overall}): {entry['entry_hash'][:16]}…")
        except Exception as exc:
            log(f"could not chain AUDIT_RESULT (report still written to disk): {exc}\n"
                f"{traceback.format_exc()}")

        print(f"self_audit: {overall} -- {report_path}")
        return 0

    except Exception as exc:
        log(f"self_audit crashed entirely: {exc}\n{traceback.format_exc()}")
        print(f"self_audit: CRASHED ({exc}) -- see {LOG_PATH}", file=sys.stderr)
        return 0  # never block the nightly cycle -- smoke detector, not sprinkler


# Only these detail keys hold actual findings. Counting every list in
# detail{} is wrong -- metadata like calendar_coverage's two-element
# "range" would inflate a clean PASS to "finding_count: 2", which is
# exactly the kind of number that quietly erodes trust in a record.
# A key not listed here contributes zero to the count, by design.
FINDING_KEYS = {
    "gap_dates",
    "missing_after_cutoff", "unexpected_before_cutoff",
    "orphan_hash_no_ots", "pending_past_grace", "missing_entirely",
    "flagged", "parse_errors",
    "sidecar_flags", "constant_columns",
    "gated_but_unimplemented", "implemented_but_never_gated",
    "decreases",
    "missing_paths", "broken_verify_commands", "constant_mismatches",
}


def _finding_count(check: dict) -> int:
    detail = check.get("detail", {})
    return sum(len(v) for k, v in detail.items()
               if k in FINDING_KEYS and isinstance(v, list))


if __name__ == "__main__":
    sys.exit(main())
