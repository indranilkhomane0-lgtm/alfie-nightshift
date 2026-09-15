"""
Alfie Night Shift — strategy_version.

A content hash over exactly the files that can change what signal gets
emitted on a given night: the call-determining surface, as opposed to
code_version (nightshift/publish_chain.py), which is the git HEAD sha and
therefore moves on every commit -- including infrastructure/tooling
commits that touch zero strategy logic. That conflation is what entry 234
froze against and what the entry-following-it correction repoints away
from: see reports/chain.jsonl for both.

STRATEGY_VERSION_FILES was derived by tracing the real nightly data flow
in nightshift/cycle.py (NightShiftCycle._stages) from price fetch through
to the direction/entry_price/settle_date written to predictions.jsonl --
a file is on this list if there is a real code path from it to that
stamped row, OR (meta_model.py only, see below) if leaving it off would
let a live wiring change move the system without moving the hash. Traced
in, with the causal link:

  config.py            -- every threshold gating the pipeline (assets,
                           regime eligibility/sizing, WFO search bounds,
                           GT-Score weights, all 5 MC gate thresholds).
                           Hashed whole -- a few non-causal constants
                           (META_*, DECAY_*, KELLY_FRACTION) ride along
                           in the same file; over-inclusion here is the
                           safe direction, not a defect.
  regime_engine.py      -- decides eligible_families, which directly
                           gates which strategy families are even tried.
  strategies/__init__.py -- the strategy fns and their optuna param
                           search spaces; used by both WFO and MC gating.
  wfo_engine.py          -- WFOEngine.optimise produces each candidate's
                           params/sharpe_oos/config_id.
  mc_gate.py             -- the 5-gate MC filter deciding which
                           candidates survive to be stamped at all.
  stamp_prediction.py    -- entry_signal()/_rsi()/_ma()/_zscore(): the
                           actual code deciding tonight's long/none. A
                           hand-mirrored reimplementation of
                           strategies/__init__.py's entry conditions, not
                           a shared call -- both must move together or
                           this list silently goes stale.
  cycle.py                -- the orchestrator: stage order, the
                           trials-per-(family,asset) formula, which fn
                           gets called with what args. Glue code is as
                           signal-determining as the logic it calls.
  meta_model.py           -- added 2026-09-16, NOT for a causal reason --
                           traced and confirmed non-causal today, same
                           as when first excluded (see reports/chain.jsonl
                           for the correction entry naming this reversal):
                           stamp() still runs on every mc_passed config
                           regardless of meta-model rank order, and
                           LiveMonitor.register() is still never called
                           from cycle.py. Included anyway as a
                           conservative buffer: the day register() gets
                           wired to gate deployment on meta-rank, that
                           wiring change would move what the system
                           actually does without this file on the list --
                           and a change that moves the system without
                           moving the hash is exactly the failure mode
                           the freeze exists to prevent. Cheaper to carry
                           one dormant file on the list now than to catch
                           that gap after it's already live.

Traced out (confirmed non-causal, not merely deprioritised):
  live_monitor.py, db.py -- stamp() runs on every mc_passed config
    regardless of meta-model rank order; LiveMonitor.register() is never
    called from cycle.py at all (only deregister, in a branch that has
    never fired -- see README). Live-monitor status reaches the brief's
    display text and DB storage, never the stamped row. Unlike
    meta_model.py above, no conservative-buffer case was made for these
    two -- db.py is pure storage/query plumbing with no architecture of
    its own to protect, and live_monitor.py's dormant behavior (auto-
    suspend) has no analogous "gates deployment" future wiring on the
    table the way meta-model ranking does. Revisit if that changes.
  derivatives.py -- feeds cfg_dicts (funding_rate etc.) for meta-model
    training/DB storage only; entry_signal() never reads it.
  core/bar_calendar.py -- is_utc_daily_bar_complete() only shapes
    context_hash/context_start/context_end (provenance metadata);
    direction is computed off the full close series before that filter
    runs (see the comment in stamp_prediction.stamp()).
  run_nightshift.py, run_and_publish.sh -- pure CLI/orchestration
    dispatch, no signal logic of their own.
  meta_model.pkl, corpus.db -- gitignored, locally regenerated
    artifacts, not version-controlled. Hashing them would also break
    "deterministic across machines and checkouts" outright, independent
    of the causality question -- they legitimately differ by machine
    and by time as the corpus grows, with zero code change.

Not covered at all: third-party library versions (numpy/pandas/optuna/
hmmlearn/sklearn) can change output too, but that's a dependency-pinning
problem, not a content-hash-over-repo-files problem.
"""

import hashlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Every path relative to ROOT. See module docstring for the reasoning
# behind each inclusion and exclusion -- this list IS the definition of
# "the strategy," so changing it is itself a methodology change that
# belongs on the chain, not a routine edit.
STRATEGY_VERSION_FILES = [
    "nightshift/config.py",
    "nightshift/cycle.py",
    "nightshift/mc_gate.py",
    "nightshift/meta_model.py",
    "nightshift/regime_engine.py",
    "nightshift/stamp_prediction.py",
    "nightshift/strategies/__init__.py",
    "nightshift/wfo_engine.py",
]


def compute_strategy_version(root: Path = ROOT) -> str:
    """sha256 over sorted (path, content) pairs -- deterministic across
    machines and checkouts because it hashes file bytes, not a git
    commit sha (which moves on every commit, strategy or not).

    Path and a NUL separator are hashed alongside each file's content so
    two files can never be concatenated into an ambiguous boundary, and
    so an empty file still contributes a distinguishable update to the
    running hash rather than silently vanishing.
    """
    h = hashlib.sha256()
    for rel in sorted(STRATEGY_VERSION_FILES):
        content = (root / rel).read_bytes()
        h.update(rel.encode())
        h.update(b"\0")
        h.update(content)
        h.update(b"\0")
    return h.hexdigest()


if __name__ == "__main__":
    print(compute_strategy_version())
