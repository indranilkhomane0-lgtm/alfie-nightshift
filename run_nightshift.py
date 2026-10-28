#!/usr/bin/env python3
"""
run_nightshift.py — Alfie Night Shift runner

  python run_nightshift.py          # full cycle with real Binance data
  python run_nightshift.py --demo   # seed corpus + run cycle (test everything)
  python run_nightshift.py --health # corpus health report
  python run_nightshift.py --features # feature importances

  python run_nightshift.py --allow-synthetic-prices   # OFFLINE TESTING ONLY:
      # falls back to fabricated prices if Binance is unreachable, instead
      # of aborting. Never pass this for a real nightly run -- the normal
      # path aborts and lets run_and_publish.sh publish a PIPELINE_FAILURE
      # rather than gate on invented data.
"""
import sys
from pathlib import Path

# Refuse to run outside this repo's venv, checked before any other import.
# Third-party imports (numpy, pandas, ccxt, sklearn, ...) are lazy -- inside
# cmd_*() and inside nightshift.cycle -- so under the wrong interpreter this
# used to run for a moment, then crash mid-import with ModuleNotFoundError
# from inside cmd_full()'s own try/except, which writes
# nightshift/logs/last_pipeline_failure.json indistinguishable from a real
# automated cycle failure: 2026-09-25's chain entry (a genuine Binance
# timeout) inherited a stale ModuleNotFoundError classification this way,
# from an earlier same-day manual run under the wrong interpreter -- the
# sidecar's staleness check only compares dates, not which run wrote it.
# Checking here, before argparse/logging/anything else loads, means a
# wrong-interpreter invocation exits cleanly with no sidecar at all, rather
# than leaving behind what looks like an unprocessed incident to whoever
# looks next.
_VENV_DIR = (Path(__file__).resolve().parent / "venv")
if Path(sys.prefix).resolve() != _VENV_DIR.resolve():
    print(
        f"run_nightshift.py must run under this repo's venv "
        f"({_VENV_DIR}), not {sys.executable}.\n"
        f"Run instead:\n  {_VENV_DIR}/bin/python3 {' '.join(sys.argv)}",
        file=sys.stderr,
    )
    raise SystemExit(1)

import argparse, json, logging, os, time
from datetime import date, datetime, timezone

# Sidecar for publish_chain.py --failed: written the instant cmd_full()'s
# cycle actually raises, read (and deleted) moments later in the same
# run_and_publish.sh invocation. Lets a PIPELINE_FAILURE payload carry a
# failure class and exception type without run_and_publish.sh having to
# parse a captured traceback out of a log file, and without touching
# nightshift/cycle.py (part of the strategy_version-frozen surface --
# this classification is diagnostic/logging, not signal logic, but the
# sidecar keeps the change entirely on this side of that line regardless).
# Lives under nightshift/logs/, already gitignored -- never committed,
# never meant to outlive the run that wrote it.
FAILURE_SIDECAR = Path("nightshift/logs/last_pipeline_failure.json")

# ── Run-instant preflight ─────────────────────────────────────────────
# The cycle fixes its date (cycle_id, cycle_date, the archive key) from the
# LOCAL date.today() when NightShiftCycle is built, and its closed-bar cut
# from the UTC date moments later (core/bar_calendar.py). Those agree only
# when the run starts after the UTC daily close and the local date has
# already rolled to the UTC date. 05:30 IST is 00:00:00 UTC exactly, so
# the schedule alone gave zero margin; every night's margin came from
# startup time (minimum 6s observed). A run that starts before the close
# builds night D+1 on a window ending D-1 -- see the METHODOLOGY_CHANGE
# carrying INPUT_WINDOW_MARKER_KEY in nightshift/self_audit.py.
#
# Refusal exits PREFLIGHT_EXIT before any side effect (no DB, no stamp, no
# brief, no failure sidecar). run_and_publish.sh alerts and publishes no
# PIPELINE_FAILURE -- that entry would be keyed to a UTC date already
# settled. The next fire retries. Keep MIN_SECONDS_AFTER_UTC_CLOSE in sync
# with nightshift/self_audit.py, which re-checks it from the chain.
PREFLIGHT_EXIT = 3
MIN_SECONDS_AFTER_UTC_CLOSE = 30


def _local_tz_name() -> str:
    """IANA name from /etc/localtime's symlink target (macOS), else the
    abbreviation Python reports. Never raises."""
    try:
        target = os.readlink("/etc/localtime")
        if "zoneinfo/" in target:
            return target.split("zoneinfo/", 1)[1]
    except OSError:
        pass
    return time.tzname[0] if time.tzname else "unknown"


def run_instant(now_utc: datetime | None = None, local_date: date | None = None) -> dict:
    """The run's start instant, as recorded in brief_<id>.run.json.
    local_date defaults to the same instant read in the system timezone,
    which is what date.today() inside the cycle returns."""
    now = now_utc or datetime.now(timezone.utc)
    close = now.replace(hour=0, minute=0, second=0, microsecond=0)
    local = local_date or now.astimezone().date()
    return {
        "run_started_at_utc": now.isoformat(),
        "seconds_after_utc_close": round((now - close).total_seconds(), 3),
        "utc_date_at_start": now.date().isoformat(),
        "local_date_at_start": local.isoformat(),
        "local_tz": _local_tz_name(),
    }


def preflight_refusal(instant: dict) -> str | None:
    """Why this instant must not build a cycle, or None if it may."""
    if instant["seconds_after_utc_close"] < MIN_SECONDS_AFTER_UTC_CLOSE:
        return (f"only {instant['seconds_after_utc_close']}s past the UTC daily "
                f"close; need >= {MIN_SECONDS_AFTER_UTC_CLOSE}s")
    if instant["local_date_at_start"] != instant["utc_date_at_start"]:
        return (f"local date {instant['local_date_at_start']} ({instant['local_tz']}) "
                f"differs from UTC date {instant['utc_date_at_start']}: cycle_id "
                f"would not name the night the closed-bar window belongs to")
    return None


def _write_run_sidecar(brief_path: Path, instant: dict) -> None:
    """brief_<id>.run.json next to the brief; publish_chain.py --brief
    chains it as payload["run"]. Never raises -- a missing sidecar shows
    up as a self_audit.py check 13 finding, not a failed night."""
    try:
        brief_path.with_suffix(".run.json").write_text(
            json.dumps(instant, sort_keys=True, indent=2))
    except Exception as exc:
        log.error("could not write run sidecar for %s: %s", brief_path.name, exc)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(f"nightshift/logs/cycle_{date.today()}.log"),
    ],
)
log = logging.getLogger("run_nightshift")

def cmd_demo(args):
    import numpy as np
    from nightshift.db import init_db, insert_config_entry, update_live_outcome
    from nightshift.meta_model import get_meta_model
    from nightshift.cycle import NightShiftCycle
    init_db()
    log.info("Seeding corpus with 60 synthetic outcomes …")
    rng = np.random.default_rng(42)
    for i in range(60):
        wfo_sh = float(rng.uniform(0.5,2.5))
        regime = int(rng.choice([0,1,2,3],p=[0.45,0.25,0.20,0.10]))
        g1=float(rng.uniform(0.1,0.9)); g2=float(rng.uniform(0.1,0.8))
        g3=float(rng.uniform(-0.1,0.7)); g4=float(rng.uniform(0.1,0.8))
        g5=float(rng.uniform(0.0,0.5)); mc=(g1+g2+g3+g4+(1-g5))/5
        logit = (wfo_sh-1.2)+mc-0.3*regime
        surv  = bool(rng.random() < 1/(1+np.exp(-logit)))
        decay = float(rng.beta(3,2)*(1.2 if surv else 0.5))
        dep   = f"2025-{(i//30)+1:02d}-{(i%30)+1:02d}"
        cfg   = {"config_id":f"DEMO_{i:04d}","asset":"BTC/USDT",
                 "strategy_family":str(rng.choice(["momentum","mean_rev","relative"])),
                 "deployment_date":dep,
                 "wfo_sharpe":wfo_sh,"wfo_sortino":wfo_sh*1.2,"wfo_calmar":wfo_sh*0.8,
                 "wfo_max_dd":float(-rng.uniform(0.05,0.20)),
                 "wfo_win_rate":float(rng.uniform(0.40,0.65)),
                 "wfo_gt_score":wfo_sh*0.85,"wfo_n_trades":int(rng.integers(20,80)),
                 "mc_gate1_p10":g1,"mc_gate2_p10":g2,"mc_gate3_p10":g3,
                 "mc_gate4_p10":g4,"mc_gate5_sensitivity":g5,"mc_composite":mc,
                 "regime_state":regime,"regime_prob":float(rng.uniform(0.6,0.95)),
                 "regime_days_in":int(rng.integers(3,30)),
                 "regime_transition_p":float(rng.uniform(0.05,0.20)),
                 "funding_rate":float(rng.normal(0.01,0.04)),
                 "oi_trend_7d":float(rng.normal(0.0,0.08)),
                 "exchange_flow_7d":float(rng.normal(0.0,0.05)),
                 "vol_ratio":float(rng.uniform(0.7,1.5)),
                 "longshort_ratio":float(rng.uniform(0.4,0.7)),
                 "btc_dominance_delta":float(rng.normal(0.0,0.01)),"params":{}}
        insert_config_entry(0, cfg)
        update_live_outcome(cfg["config_id"],dep,wfo_sh*decay,
                            cfg["wfo_max_dd"]*float(rng.uniform(0.5,1.2)),
                            cfg["wfo_n_trades"],decay,surv,"demo seed")
    log.info("Training meta-model …")
    mm = get_meta_model(); ok = mm.train(force=True)
    if ok:
        log.info("Trained — AUC=%.3f  R²=%.3f", mm._cv_clf, mm._cv_reg)
        print("\n── Feature importances ─────────────────────────────────────────")
        for f,v in mm.top_features(10):
            print(f"  {f:35s}  {v:.4f}  {'█'*int(v*60)}")
    log.info("\nRunning full cycle …")
    brief = Path(NightShiftCycle(
        allow_synthetic_prices=args.allow_synthetic_prices).run()).read_text()
    print("\n" + brief)

def _innermost_named_exception(exc: Exception) -> Exception:
    """Walks __cause__ (explicit 'raise X from Y') then __context__
    (implicit -- raised while already handling another exception) down
    to the deepest exception that still names something specific,
    stopping just before descending into a bare stdlib/socket-level
    error (socket.gaierror, ValueError, OSError, ...) that carries no
    more diagnostic value than the library-level exception already
    wrapping it. For example: urllib3.exceptions.NameResolutionError
    already says "DNS resolution failed"; its own __cause__ is a plain
    socket.gaierror, which says less, not more -- the walk stops at
    NameResolutionError, not gaierror.

    This exists because cycle.py's load_price_data() raises exactly one
    RuntimeError on the nightly path, always with the same message
    shape ("price fetch failed for {asset}..."), regardless of what
    actually went wrong underneath it -- a DNS resolution failure
    (urllib3.exceptions.NameResolutionError) and a mid-transfer
    truncation (requests.exceptions.ChunkedEncodingError /
    urllib3.exceptions.ProtocolError) both collapsed into the identical
    (upstream_data, RuntimeError) pair on the chain, indistinguishable
    after the fact (disclosed: four PIPELINE_FAILURE entries, 2026-10-15/
    18/20/21, the first three genuinely DNS, the fourth a different
    truncation entirely, all recorded the same way). Never raises;
    worst case returns exc itself unchanged."""
    current = exc
    while True:
        nxt = current.__cause__ or current.__context__
        if nxt is None or type(nxt).__module__.split(".")[0] in ("builtins", "socket"):
            return current
        current = nxt


def _classify_failure(exc: Exception) -> str:
    """Coarse, best-effort category for the chained record -- enough to
    separate an upstream data/exchange outage from a bug in our own
    pipeline code, not a full diagnosis. RuntimeError is raised exactly
    once on the nightly path, by cycle.load_price_data(), specifically
    when the exchange fetch fails (see that function's docstring: "price
    fetch failed for {asset}, refusing to proceed on fabricated data").
    Matching on that message is fragile in the abstract, but it's the
    one and only RuntimeError site in the real nightly path, so a match
    failure here just falls back to "internal" -- never crashes, never
    fabricates a more specific answer than the evidence supports."""
    if isinstance(exc, RuntimeError) and "price fetch failed" in str(exc):
        return "upstream_data"
    return "internal"


def _write_failure_sidecar(exc: Exception) -> None:
    """Best-effort -- read moments later, in the same run_and_publish.sh
    invocation, by publish_chain.py --failed. Never raises: a failure
    here must not mask or replace the real exception already propagating
    out of cmd_full().

    run_id: run_and_publish.sh exports ALFIE_RUN_ID once per invocation,
    inherited here and by the publish_chain.py --failed call that follows
    in the same shell. Stamping it into the sidecar lets the reader bind
    a sidecar to the specific run that wrote it, not just today's date --
    see chain entry 285 for what a same-day-only check missed (a stale
    sidecar from an unrelated manual run got read as this run's own).
    Absent (manual invocation, no wrapper script) -> "unknown", same
    fail-safe placeholder used elsewhere in this file.

    exception_type records _innermost_named_exception(exc)'s class,
    not exc's own -- exc here is always cycle.py's RuntimeError wrapper
    (see _classify_failure's docstring), identical in shape regardless
    of what actually failed underneath it. failure_class still reads
    exc directly, unchanged -- the wrapper is exactly what
    _classify_failure's message-matching needs, and "upstream_data"
    covers both a DNS failure and a truncated transfer correctly; the
    distinction belongs in exception_type, not in a second
    failure_class."""
    try:
        FAILURE_SIDECAR.parent.mkdir(parents=True, exist_ok=True)
        FAILURE_SIDECAR.write_text(json.dumps({
            "exception_type": type(_innermost_named_exception(exc)).__name__,
            "failure_class": _classify_failure(exc),
            "written_at_utc": datetime.now(timezone.utc).isoformat(),
            "run_id": os.environ.get("ALFIE_RUN_ID", "unknown"),
        }))
    except Exception:
        pass


def cmd_full(args):
    instant = run_instant()
    refusal = preflight_refusal(instant)
    if refusal:
        log.error("PREFLIGHT REFUSED -- %s (started %s). No cycle built.",
                  refusal, instant["run_started_at_utc"])
        print(f"PREFLIGHT REFUSED: {refusal}", file=sys.stderr)
        raise SystemExit(PREFLIGHT_EXIT)
    try:
        from nightshift.db import init_db
        from nightshift.cycle import NightShiftCycle
        init_db()
        cycle = NightShiftCycle(allow_synthetic_prices=args.allow_synthetic_prices)
        instant["cycle_id"] = cycle.cycle_id
        brief_path = Path(cycle.run())
        _write_run_sidecar(brief_path, instant)
        print("\n" + brief_path.read_text())
    except Exception as exc:
        # cycle.py already logs+re-raises (see NightShiftCycle.run()) --
        # this only adds the sidecar, then lets the exception keep
        # propagating exactly as before, so run_and_publish.sh's exit-code
        # check is unaffected.
        _write_failure_sidecar(exc)
        raise

def cmd_health(args):
    from nightshift.meta_model import get_meta_model
    h = get_meta_model().corpus_health()
    print("\n── CORPUS HEALTH ───────────────────────────────────────────────")
    for k,v in h.items():
        print(f"  {k:30s}: {v:.4f}" if isinstance(v,float) else f"  {k:30s}: {v}")

def cmd_features(args):
    from nightshift.meta_model import get_meta_model
    mm = get_meta_model()
    if not mm._trained: print("Model not trained yet. Run --demo first."); return
    print("\n── FEATURE IMPORTANCES ─────────────────────────────────────────")
    for f,v in mm.top_features(22):
        print(f"  {f:35s}  {v:.4f}  {'█'*int(v*60)}")

if __name__ == "__main__":
    p = argparse.ArgumentParser()
    g = p.add_mutually_exclusive_group()
    g.add_argument("--demo",     action="store_true")
    g.add_argument("--health",   action="store_true")
    g.add_argument("--features", action="store_true")
    p.add_argument("--allow-synthetic-prices", action="store_true",
        dest="allow_synthetic_prices",
        help="Offline testing only: fall back to fabricated price series "
             "if Binance is unreachable, instead of aborting the cycle. "
             "Never set this for a real nightly run.")
    args = p.parse_args()
    if args.demo:     cmd_demo(args)
    elif args.health: cmd_health(args)
    elif args.features: cmd_features(args)
    else: cmd_full(args)
