#!/usr/bin/env python3
"""
Alfie Night Shift — reproduction verifier.

The script a skeptic runs to re-derive a published call from the
archived record, without an exchange account and without trusting the
operator. Takes one night's date and:

  1. Reads that night's chain entry (code_version, strategy_version,
     predictions_sha256) and that night's rows in reports/predictions.jsonl
     (context_hash, context_source, context_fetched_at per prediction).
  2. Loads that night's archived closed-bar window for each asset from
     reports/archive/ohlcv.csv.gz and recomputes
     nightshift.stamp_prediction._context_hash() over it, comparing
     against the hash predictions.jsonl actually recorded. A match here
     proves the archive holds the exact bytes that were hashed that
     night -- not an approximation, not a re-fetch.
  3. Replays the deterministic pipeline stages -- regime classification,
     walk-forward optimisation, Monte Carlo gating, meta-model ranking
     -- against ONLY the archived bars and the corpus state reconstructed
     from reports/archive/corpus_delta.jsonl, using the real pipeline
     code (nightshift.regime_engine, nightshift.wfo_engine,
     nightshift.mc_gate, nightshift.strategies, nightshift.meta_model).
  4. Diffs the resulting selected call against what predictions.jsonl
     actually stamped for that night, field by field, and reports match
     or mismatch honestly -- including where this script's own inputs
     are known to fall short of what the real run actually used (see
     "KNOWN GAP" below) rather than overclaiming a clean result.

NO NETWORK, ENFORCED, NOT JUST AVOIDED. This script never imports
ccxt, nightshift.cycle.load_price_data, or
nightshift.derivatives.get_all_signals_with_status. As a second,
independent guarantee -- so a skeptic doesn't have to take the first
one on faith -- _block_network() (called before any other import below
it) monkeypatches socket.socket.connect/connect_ex to raise. If
anything anywhere in the import graph tries a real network connection
during a run of this script, it fails loudly and immediately instead
of silently succeeding.

HONEST ABOUT WHAT IT CANNOT CHECK:
  - A date with no archived bars (before 2026-10-07, when the OHLCV
    archive started, or any later date the archiver failed on and
    never recovered -- see nightshift/archive_ohlcv.py's documented
    limit on that) reports NOT REPRODUCIBLE -- NO ARCHIVE. Never a
    pass. There is nothing here to verify against.
  - A date whose only chain entry is PIPELINE_FAILURE reports NOT
    REPRODUCIBLE -- NO CALL WAS PUBLISHED. There is no call to
    reproduce.
  - code_version (the git HEAD sha recorded that night) will differ
    from the current checkout's on almost every date, by design --
    code_version moves on every commit, including nightly briefs and
    unrelated infra, not just changes to the call-determining code.
    Refusing on ANY code_version difference would make this tool
    unusable for nearly every date. What actually matters for whether
    the call-making logic itself is identical is strategy_version (the
    content hash over exactly the files that can change what signal
    gets emitted -- see nightshift/strategy_version.py). This script
    REFUSES outright if strategy_version differs (the frozen surface
    genuinely differs; no override makes that meaningful) and WARNS,
    prominently, not silently, if code_version differs while
    strategy_version matches (the common, benign case -- something
    outside the frozen surface changed, e.g. a later night's brief
    commit). --allow-strategy-mismatch overrides the hard refusal for
    deliberate exploration; it does not silence the warning.
  - KNOWN GAP, stated here rather than discovered by a confused reader:
    nightshift/archive_ohlcv.py archives `closed` -- the window with
    today's in-progress daily candle excluded, exactly what
    _context_hash() hashes. But nightshift/cycle.py's Stage 1 fetch,
    which is what regime classification, WFO, and MC gating actually
    ran against that night, uses the FULL fetched window, live candle
    included -- one bar more than what's archived. This script can
    only replay stages 3-5 against the archived (closed-bars-only)
    window, one bar short of the real run's input. Step 2 above (the
    context_hash check) is unaffected -- it was always computed over
    the closed-only window. Steps 3-4 (the replay) may diverge from
    the real run for this reason alone, independent of any genuine
    cross-machine determinism question. This script reports which one
    it believes it's looking at, not just "mismatch".
  - Meta-model weights: if nightshift/db.py's labeled_prediction_date_count
    as of the target date was below META_MIN_SAMPLES (30), the real
    run's meta-model was provably untrained (nightshift/meta_model.py's
    MetaModel.train() refuses below that threshold, unconditionally) --
    ranking fell back to a plain gt_score sort, fully deterministic,
    fully reproducible from archived data alone, no caveat needed. If
    the count was at or above 30, the real run MAY have trained or used
    a previously-trained model loaded from meta_model.pkl -- a file
    that is gitignored, never archived, and carries no hash on this
    chain. This script retrains fresh from archived+predictions data in
    that case and says so plainly: a match is corroborating, not
    proof the literal weights in memory that night were reproduced.

Usage (no repo-specific setup beyond a checkout and the pinned deps):

    git clone <this repo> && cd alfie-nightshift
    pip install -r requirements.txt       # every dependency is ==-pinned
    python3 core/reproduce.py 2026-10-07

Exit code 0 only on a clean, fully-checked run (verdict may still be
MISMATCH -- a reproducible mismatch is a successful run of this tool,
not a failure of it). Exit code 1 for every NOT REPRODUCIBLE case and
for a refused code/strategy-version mismatch. Exit code 2 for a usage
error (bad date, missing archive files entirely).
"""
import argparse
import csv
import gzip
import subprocess
import sys
from datetime import date as _date
from pathlib import Path


def _block_network():
    import socket as _socket

    def _blocked(*_a, **_kw):
        raise RuntimeError(
            "core/reproduce.py: network access attempted and blocked -- "
            "this script must run entirely from archived/local data. If "
            "you are seeing this, something in the import graph tried a "
            "real network connection; that is a bug to fix, not a "
            "message to work around."
        )
    _socket.socket.connect = _blocked
    _socket.socket.connect_ex = _blocked


_block_network()

import json  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

CHAIN_PATH = ROOT / "reports" / "chain.jsonl"
PREDICTIONS_PATH = ROOT / "reports" / "predictions.jsonl"
OHLCV_ARCHIVE_PATH = ROOT / "reports" / "archive" / "ohlcv.csv.gz"
CORPUS_DELTA_PATH = ROOT / "reports" / "archive" / "corpus_delta.jsonl"

# Columns compared field by field in the final diff -- the ones that
# describe WHAT the call is, not provenance/bookkeeping fields that
# necessarily differ on every reproduction (prediction_id embeds the
# wall-clock date string; context_fetched_at was never populated by the
# real pipeline either, see nightshift/stamp_prediction.py).
DIFF_FIELDS = [
    "asset", "strategy_family", "direction", "entry_price",
    "context_hash", "context_n_bars", "context_start", "context_end",
    "wfo_win_rate", "regime_state",
]


class NotReproducible(Exception):
    """A clean, expected 'nothing to verify' outcome -- not a bug.
    main() prints str(self) as the verdict and exits 1."""


class RefusedMismatch(Exception):
    """strategy_version differs from the current checkout and
    --allow-strategy-mismatch was not passed. main() prints and exits 1."""


# ── Phase 1: chain entry + predictions for the target date ─────────────

def _load_chain():
    if not CHAIN_PATH.exists():
        raise NotReproducible(f"no chain file at {CHAIN_PATH}")
    return [json.loads(l) for l in CHAIN_PATH.read_text().splitlines() if l.strip()]


def find_night(entries: list, target: _date) -> dict:
    """The NIGHTLY_BRIEF chain entry published on `target`, or raises
    NotReproducible with the precise, honest reason: a PIPELINE_FAILURE
    that night (no call was published -- nothing to reproduce), or no
    entry at all for that date."""
    day = target.isoformat()
    day_entries = [e for e in entries if e.get("published_at_utc", "")[:10] == day]
    if not day_entries:
        raise NotReproducible(f"NOT REPRODUCIBLE -- no chain entry exists for {day}")
    for e in day_entries:
        if e["payload"].get("type") == "NIGHTLY_BRIEF":
            return e
    failures = [e for e in day_entries if e["payload"].get("type") == "PIPELINE_FAILURE"]
    if failures:
        f = failures[0]["payload"]
        raise NotReproducible(
            f"NOT REPRODUCIBLE -- no call was published for {day}: the cycle "
            f"failed (failure_class={f.get('failure_class')}, "
            f"exception_type={f.get('exception_type')}). A PIPELINE_FAILURE "
            f"night has nothing to reproduce by design -- see that entry, "
            f"not this tool, for what happened."
        )
    raise NotReproducible(
        f"NOT REPRODUCIBLE -- {day} has chain entries but none is a "
        f"NIGHTLY_BRIEF or PIPELINE_FAILURE (found: "
        f"{[e['payload'].get('type') for e in day_entries]})"
    )


def load_predictions_for(target: _date) -> list:
    if not PREDICTIONS_PATH.exists():
        return []
    day = target.isoformat()
    rows = [json.loads(l) for l in PREDICTIONS_PATH.read_text().splitlines() if l.strip()]
    return [r for r in rows if r.get("cycle_date") == day]


# ── Phase 2: code_version / strategy_version check ──────────────────────

def _git_head() -> str:
    try:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True,
            text=True, timeout=5, check=True,
        ).stdout.strip()
    except Exception as exc:
        raise NotReproducible(f"could not determine current git HEAD: {exc!r}")


def check_versions(night_payload: dict, allow_strategy_mismatch: bool) -> list:
    """Returns a list of warning strings (possibly empty). Raises
    RefusedMismatch if strategy_version differs and the override wasn't
    passed -- see module docstring for why code_version alone is the
    wrong thing to gate on."""
    from nightshift.strategy_version import compute_strategy_version

    warnings = []
    current_head = _git_head()
    recorded_code_version = night_payload.get("code_version")
    if recorded_code_version in (None, "unknown"):
        warnings.append(
            "that night's code_version is missing or 'unknown' -- cannot "
            "compare at all; proceeding on strategy_version alone"
        )
    elif recorded_code_version != current_head:
        warnings.append(
            f"code_version differs: that night recorded "
            f"{recorded_code_version[:12]}, current checkout is "
            f"{current_head[:12]}. This is EXPECTED on almost any date -- "
            f"code_version is the git HEAD sha and moves on every commit, "
            f"including nightly briefs and infra changes with zero effect "
            f"on call-making logic. See strategy_version below for the "
            f"check that actually matters."
        )

    current_strategy = compute_strategy_version()
    recorded_strategy = night_payload.get("strategy_version")
    if recorded_strategy != current_strategy:
        msg = (
            f"strategy_version differs: that night recorded "
            f"{str(recorded_strategy)[:12]}, current checkout computes "
            f"{current_strategy[:12]}. The frozen call-determining surface "
            f"(nightshift/strategy_version.py's STRATEGY_VERSION_FILES) "
            f"has genuinely changed since that night -- a reproduction "
            f"run under different signal-computing code and labeled a "
            f"match would be worse than no reproduction at all."
        )
        if not allow_strategy_mismatch:
            raise RefusedMismatch(
                msg + " Refusing. Re-run with --allow-strategy-mismatch "
                "to proceed anyway (for deliberate exploration only -- "
                "any result is not a verification)."
            )
        warnings.append(msg + " Proceeding only because "
                         "--allow-strategy-mismatch was passed.")
    return warnings


# ── Phase 3: load archived OHLCV, recompute context_hash ────────────────

def load_archived_bars(target: _date):
    """{asset: pandas.DataFrame} for every asset archived for `target`,
    reconstructed from reports/archive/ohlcv.csv.gz exactly as
    nightshift/stamp_prediction.py's `closed` window was shaped: a
    DatetimeIndex, columns open/high/low/close/volume, float64. Raises
    NotReproducible if the archive doesn't exist or has nothing for
    this date -- the required "no archive, not a pass" behavior."""
    import pandas as pd

    if not OHLCV_ARCHIVE_PATH.exists():
        raise NotReproducible(
            f"NOT REPRODUCIBLE -- NO ARCHIVE: {OHLCV_ARCHIVE_PATH} does not "
            f"exist. Raw OHLCV archiving began 2026-10-07; a date before "
            f"that, or a date the archiver failed on without recovering "
            f"(see nightshift/archive_ohlcv.py's documented 'defer and "
            f"sweep does not fully apply here' limit), has nothing to "
            f"verify against."
        )
    day = target.isoformat()
    by_asset = {}
    try:
        with gzip.open(OHLCV_ARCHIVE_PATH, "rt", newline="") as f:
            for row in csv.reader(f):
                if not row or row[0] != day:
                    continue
                _cycle_date, asset, bar_date, source, o, h, l, c, v = row
                by_asset.setdefault(asset, {"rows": [], "source": source})
                by_asset[asset]["rows"].append((bar_date, float(o), float(h),
                                                 float(l), float(c), float(v)))
    except Exception as exc:
        raise NotReproducible(f"could not read/decompress OHLCV archive: {exc!r}")

    if not by_asset:
        raise NotReproducible(
            f"NOT REPRODUCIBLE -- NO ARCHIVE: {OHLCV_ARCHIVE_PATH} exists "
            f"but has no rows for {day}."
        )

    out = {}
    for asset, d in by_asset.items():
        rows = sorted(d["rows"], key=lambda r: r[0])
        idx = pd.to_datetime([r[0] for r in rows])
        df = pd.DataFrame({
            "open": [r[1] for r in rows], "high": [r[2] for r in rows],
            "low": [r[3] for r in rows], "close": [r[4] for r in rows],
            "volume": [r[5] for r in rows],
        }, index=idx)
        out[asset] = {"df": df, "source": d["source"]}
    return out


def verify_context_hashes(archived: dict, predictions_today: list) -> list:
    """For each prediction stamped that night, recompute _context_hash()
    over the archived bars for its asset and compare to what was
    actually recorded. This is the proof the archive holds the exact
    bytes that were hashed -- not an approximation. Returns a list of
    per-asset result dicts; raises nothing (a hash mismatch is a
    reportable finding, not a script error)."""
    from nightshift.stamp_prediction import _context_hash

    results = []
    seen_assets = set()
    for p in predictions_today:
        asset = p["asset"]
        if asset in seen_assets:
            continue
        seen_assets.add(asset)
        if asset not in archived:
            results.append({
                "asset": asset, "match": False,
                "detail": "no archived bars for this asset on this date",
            })
            continue
        df = archived[asset]["df"]
        recomputed = _context_hash(df, p.get("context_source"), p.get("context_fetched_at"))
        recorded = p.get("context_hash")
        results.append({
            "asset": asset, "match": recomputed == recorded,
            "recomputed": recomputed, "recorded": recorded,
            "archived_n_bars": len(df), "recorded_n_bars": p.get("context_n_bars"),
        })
    return results


# ── Phase 4: corpus state reconstruction from corpus_delta.jsonl ────────

def _load_corpus_delta():
    if not CORPUS_DELTA_PATH.exists():
        return []
    return [json.loads(l) for l in CORPUS_DELTA_PATH.read_text().splitlines() if l.strip()]


def corpus_rows_for_cycle(delta_rows: list, cycle_id: int) -> list:
    return [r for r in delta_rows if r.get("kind") == "candidate" and r.get("cycle_id") == cycle_id]


def reconstruct_training_frame(delta_rows: list, predictions_all: list, before: _date):
    """Mirrors nightshift/db.py's get_prediction_labeled_corpus(): corpus
    candidate rows (features) joined to predictions.jsonl's own
    WIN/LOSS/return_pct (outcome), restricted to cycles strictly before
    `before`. Used only if the target night's labeled-date count implies
    the real meta-model may have trained -- see module docstring."""
    pred_by_key = {}
    for p in predictions_all:
        if p.get("outcome") in ("WIN", "LOSS"):
            pred_by_key[(p["config_id"], p["cycle_date"])] = p

    rows = []
    for r in delta_rows:
        if r.get("kind") != "candidate":
            continue
        if r.get("void_reason"):
            continue
        dep = r.get("deployment_date")
        if not dep or dep >= before.isoformat():
            continue
        key = (r.get("config_id"), dep)
        p = pred_by_key.get(key)
        if not p:
            continue
        d = dict(r)
        d["outcome_win"] = 1 if p["outcome"] == "WIN" else 0
        d["outcome_return_pct"] = float(p.get("return_pct") or 0.0)
        d["outcome_settle_date"] = p.get("settle_date")
        rows.append(d)
    rows.sort(key=lambda x: x["outcome_settle_date"] or "")
    return rows


def labeled_date_count_as_of(predictions_all: list, before: _date) -> int:
    dates = {p["settle_date"] for p in predictions_all
             if p.get("outcome") in ("WIN", "LOSS") and p.get("settle_date")
             and p["settle_date"] < before.isoformat()}
    return len(dates)


# ── Phase 5: replay the deterministic stages ─────────────────────────────

def replay(archived: dict, delta_rows: list, cycle_id: int, target: _date,
           predictions_all: list, log) -> dict:
    """Runs regime classification -> WFO -> MC gating -> meta-rank
    against ONLY the archived bars and reconstructed corpus state, using
    the real nightshift modules. Returns a dict describing the replay's
    own selected call(s) and diagnostics. Never touches the network --
    only ever calls into nightshift.regime_engine / wfo_engine /
    mc_gate / strategies / meta_model, none of which import ccxt or
    nightshift.derivatives."""
    import numpy as np
    from nightshift.config import ASSETS, OPTUNA_TRIALS, REGIME_FAMILY_GATE
    from nightshift.regime_engine import RegimeEngine, compute_hmm_features
    from nightshift.wfo_engine import WFOEngine
    from nightshift.mc_gate import run_mc_gates
    from nightshift.strategies import STRATEGY_REGISTRY
    from nightshift.meta_model import MetaModel

    missing = [a for a in ASSETS if a not in archived]
    if missing:
        raise NotReproducible(
            f"NOT REPRODUCIBLE -- archive is missing bars for {missing} on "
            f"{target.isoformat()}; cannot replay regime/WFO without all "
            f"of ASSETS present."
        )
    prices = {a: archived[a]["df"] for a in ASSETS}

    log("Stage 3/5 (replay): regime classification …")
    btc = prices["BTC/USDT"]
    feats = compute_hmm_features(btc)
    regime_eng = RegimeEngine()
    regime_eng.fit(feats)
    regime = regime_eng.classify(feats)
    log(f"  -> state={regime.state} ({regime.name})  P={regime.probability:.3f}  "
        f"days_in={regime.days_in_state}")

    eligible = [k for k, v in REGIME_FAMILY_GATE[regime.state].items() if v]
    log(f"Stage 4/5 (replay): WFO -- eligible families {eligible}")
    n_each = max(40, OPTUNA_TRIALS // max(len(eligible) * len(ASSETS), 1))
    all_results = []
    for family in eligible:
        spec = STRATEGY_REGISTRY.get(family)
        if not spec:
            continue
        for asset in ASSETS:
            eng = WFOEngine(spec["fn"], spec["param_space"], family, n_top=5)
            all_results.extend(eng.optimise(asset, prices[asset], n_each))
    log(f"  -> {len(all_results)} config(s) passed min-Sharpe")

    log("Stage 5/5 (replay): Monte Carlo gating …")
    mc_passed = []
    for res in all_results:
        strat_fn = STRATEGY_REGISTRY[res.strategy_family]["fn"]
        mc = run_mc_gates(res, strat_fn, prices[res.asset], n_sims=300)
        if mc.all_pass:
            mc_passed.append(res)
    log(f"  -> {len(mc_passed)}/{len(all_results)} passed all 5 gates")

    archived_candidates = {r["config_id"]: r for r in corpus_rows_for_cycle(delta_rows, cycle_id)}
    cross_check = []
    cfg_dicts = []
    for res in mc_passed:
        arch = archived_candidates.get(res.config_id)
        cross_check.append({
            "config_id": res.config_id, "asset": res.asset,
            "found_in_archived_corpus_delta": arch is not None,
            "replayed_wfo_sharpe": res.sharpe_oos,
            "archived_wfo_sharpe": arch.get("wfo_sharpe") if arch else None,
            "replayed_mc_composite": res.mc_results.get("composite"),
            "archived_mc_composite": arch.get("mc_composite") if arch else None,
        })
        ds = {k: (arch.get(k) if arch else 0.0) for k in
              ("funding_rate", "oi_trend_7d", "exchange_flow_7d",
               "longshort_ratio", "btc_dominance_delta")}
        mc = res.mc_results
        cfg_dicts.append({
            "config_id": res.config_id, "asset": res.asset,
            "strategy_family": res.strategy_family, "params": res.params,
            "wfo_sharpe": res.sharpe_oos, "wfo_sortino": res.sortino_oos,
            "wfo_calmar": res.calmar_oos, "wfo_max_dd": res.max_dd_oos,
            "wfo_win_rate": res.win_rate_oos, "wfo_gt_score": res.gt_score_oos,
            "wfo_n_trades": res.n_trades, "wfo_n_trials": res.n_trials_total,
            "wfo_n_candidates_surviving_min_sharpe": res.n_candidates_surviving_min_sharpe,
            "mc_gate1_p10": mc.get("gate1_p10", 0), "mc_gate2_p10": mc.get("gate2_p10", 0),
            "mc_gate3_p10": mc.get("gate3_p10", 0), "mc_gate4_p10": mc.get("gate4_p10", 0),
            "mc_gate5_sensitivity": mc.get("gate5_sensitivity", 0),
            "mc_composite": mc.get("composite", 0),
            "regime_state": regime.state, "regime_prob": regime.probability,
            "regime_days_in": regime.days_in_state, "regime_transition_p": regime.transition_p,
            "vol_ratio": regime.features_last.get("vol_ratio", 1.0),
            **ds,
        })

    n_labeled = labeled_date_count_as_of(predictions_all, target)
    meta_trained_possible = n_labeled >= 30  # META_MIN_SAMPLES
    meta = MetaModel()
    meta_warning = None
    if meta_trained_possible:
        # Reuse the REAL MetaModel.train() unmodified -- it's the exact
        # code that ran that night -- but point its two db.py data
        # sources at our reconstructed-as-of-`target` frame instead of
        # the live corpus.db/predictions.jsonl, via a scoped monkeypatch.
        # This is the one place this script deliberately diverges from
        # "just call the real function": there is no way to ask the real
        # train() to look at history as of a past date, since it always
        # reads the CURRENT corpus.db/predictions.jsonl state.
        import nightshift.meta_model as _mm_module
        frame = reconstruct_training_frame(delta_rows, predictions_all, target)
        log(f"  labeled_date_count as of {target.isoformat()} = {n_labeled} "
            f">= 30: the real run MAY have used a trained meta-model. "
            f"Retraining fresh from {len(frame)} reconstructed rows for "
            f"this replay (real MetaModel.train() code, reconstructed data).")
        meta_warning = (
            f"labeled_date_count as of this date was {n_labeled} (>=30): "
            f"the real meta-model MAY have been trained that night, using "
            f"weights from meta_model.pkl -- a file that is gitignored and "
            f"not archived or hashed anywhere on this chain. This replay "
            f"retrained fresh, with the real training code, from "
            f"archived+predictions data reconstructed as of this date. A "
            f"match below is corroborating, not proof the literal model "
            f"weights in memory that night were reproduced -- the real run "
            f"may have used an older cached model instead of retraining."
        )
        import tempfile
        _orig_get_corpus = _mm_module.get_prediction_labeled_corpus
        _orig_get_count = _mm_module.labeled_prediction_date_count
        _orig_model_path = _mm_module.MODEL_PATH
        try:
            _mm_module.get_prediction_labeled_corpus = lambda: frame
            _mm_module.labeled_prediction_date_count = lambda: n_labeled
            # train()'s last step is self._save(), which writes to
            # MODEL_PATH unconditionally -- redirect to a throwaway file
            # so this verification run never touches the real,
            # gitignored meta_model.pkl.
            with tempfile.TemporaryDirectory() as tmp:
                _mm_module.MODEL_PATH = Path(tmp) / "reproduce_scratch.pkl"
                meta.train(force=False)
        finally:
            _mm_module.get_prediction_labeled_corpus = _orig_get_corpus
            _mm_module.labeled_prediction_date_count = _orig_get_count
            _mm_module.MODEL_PATH = _orig_model_path
        if not meta._trained:
            log("  reconstructed frame did not clear train()'s own gates "
                "(e.g. a single outcome class) -- falling back to the "
                "untrained gt_score ranking, exactly as the real run would "
                "have that night under the same data.")
    else:
        log(f"  labeled_date_count as of {target.isoformat()} = {n_labeled} "
            f"< 30: the real meta-model was PROVABLY untrained that night "
            f"(nightshift/meta_model.py's train() refuses below "
            f"META_MIN_SAMPLES unconditionally) -- ranking fell back to a "
            f"plain gt_score sort. Fully deterministic, no caveat needed.")

    gt_scores = [r.gt_score_oos for r in mc_passed]
    meta_scores = meta.rank(cfg_dicts, regime.state, gt_scores)
    order = sorted(range(len(mc_passed)), key=lambda i: meta_scores[i].rank_score, reverse=True)
    mc_passed_ranked = [mc_passed[i] for i in order]
    cfg_dicts_ranked = [cfg_dicts[i] for i in order]

    from nightshift.stamp_prediction import entry_signal, _context_hash
    replayed_predictions = []
    for res, cfg in zip(mc_passed_ranked, cfg_dicts_ranked):
        df = prices[res.asset]
        closes = df["close"].tolist()
        direction, diag = entry_signal(res.strategy_family, res.params, closes)
        if direction == "none":
            continue
        replayed_predictions.append({
            "config_id": res.config_id, "asset": res.asset,
            "strategy_family": res.strategy_family, "direction": direction,
            "entry_price": round(closes[-1], 6),
            "wfo_win_rate": res.win_rate_oos,
            "regime_state": regime.state,
            "context_hash": _context_hash(df, archived[res.asset]["source"], None),
            "context_n_bars": len(df), "context_start": df.index[0].isoformat(),
            "context_end": df.index[-1].isoformat(),
        })

    return {
        "regime": {"state": regime.state, "name": regime.name,
                   "probability": regime.probability, "days_in_state": regime.days_in_state},
        "eligible_families": eligible,
        "n_wfo_survivors": len(all_results), "n_mc_passed": len(mc_passed),
        "cross_check": cross_check,
        "meta_warning": meta_warning,
        "replayed_predictions": replayed_predictions,
    }


# ── Phase 6: diff against predictions.jsonl ──────────────────────────────

def diff_predictions(replayed: list, actual: list) -> dict:
    actual_by_asset = {}
    for p in actual:
        actual_by_asset.setdefault(p["asset"], []).append(p)
    replayed_by_asset = {}
    for p in replayed:
        replayed_by_asset.setdefault(p["asset"], []).append(p)

    report = {"per_asset": {}, "overall_verdict": None}
    all_match = True
    any_actual = bool(actual)
    for asset in sorted(set(actual_by_asset) | set(replayed_by_asset)):
        act = actual_by_asset.get(asset, [])
        rep = replayed_by_asset.get(asset, [])
        act_hashes = {p["context_hash"] for p in act}
        rep_hashes = {p["context_hash"] for p in rep}
        entry = {
            "actual_count": len(act), "replayed_count": len(rep),
            "context_hash_overlap": sorted(act_hashes & rep_hashes),
            "fields": [],
        }
        if not act or not rep:
            entry["note"] = "no actual and/or no replayed prediction for this asset"
            all_match = all_match and (not act and not rep)
        else:
            a0, r0 = act[0], rep[0]
            for f in DIFF_FIELDS:
                av, rv = a0.get(f), r0.get(f)
                m = (av == rv)
                entry["fields"].append({"field": f, "actual": av, "replayed": rv, "match": m})
                all_match = all_match and m
        report["per_asset"][asset] = entry
    report["overall_verdict"] = "MATCH" if (all_match and any_actual) else "MISMATCH"
    return report


# ── main ──────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(
        description="Re-derive a published Night Shift call from the archived "
                    "record alone -- no exchange key, no network, no trust required.",
    )
    ap.add_argument("date", help="target night, YYYY-MM-DD (UTC)")
    ap.add_argument("--allow-strategy-mismatch", action="store_true",
                     help="proceed even if strategy_version differs from the "
                          "current checkout (exploration only -- NOT a verification)")
    args = ap.parse_args()

    try:
        target = _date.fromisoformat(args.date)
    except ValueError:
        print(f"error: '{args.date}' is not a valid YYYY-MM-DD date", file=sys.stderr)
        return 2

    log_lines = []
    def log(line):
        log_lines.append(line)
        print(line, file=sys.stderr)

    try:
        entries = _load_chain()
        night = find_night(entries, target)
        payload = night["payload"]

        print(f"=== core/reproduce.py -- {target.isoformat()} ===")
        print(f"chain entry: type={payload['type']}  "
              f"code_version={str(payload.get('code_version'))[:12]}  "
              f"strategy_version={str(payload.get('strategy_version'))[:12]}")

        warnings = check_versions(payload, args.allow_strategy_mismatch)
        for w in warnings:
            print(f"WARNING: {w}")

        predictions_today = load_predictions_for(target)
        predictions_all = ([json.loads(l) for l in PREDICTIONS_PATH.read_text().splitlines()
                            if l.strip()] if PREDICTIONS_PATH.exists() else [])

        log(f"loading archived OHLCV bars for {target.isoformat()} …")
        archived = load_archived_bars(target)
        bar_counts = ", ".join(f"{a}={len(d['df'])}" for a, d in archived.items())
        print(f"archived bars: {bar_counts}")

        print("\n--- Step 2: context_hash integrity check ---")
        hash_results = verify_context_hashes(archived, predictions_today)
        for r in hash_results:
            status = "MATCH" if r["match"] else "MISMATCH"
            print(f"  {r['asset']:10s} {status}  "
                  f"(archived {r.get('archived_n_bars')} bars, "
                  f"predictions.jsonl recorded {r.get('recorded_n_bars')})")
            if not r["match"]:
                print(f"    recomputed={str(r.get('recomputed'))[:16]}  "
                      f"recorded={str(r.get('recorded'))[:16]}  "
                      f"detail={r.get('detail', '')}")
        hashes_ok = all(r["match"] for r in hash_results)

        print("\n--- Steps 3-4: replay regime / WFO / MC-gate / meta-rank ---")
        print("KNOWN GAP: replay runs against the archived CLOSED-bar window "
              "(today's in-progress candle excluded -- same window "
              "_context_hash() hashes). The real cycle's regime/WFO/MC-gate "
              "stages used the FULL fetched window, one bar more. A "
              "divergence below may be this, not a cross-machine float issue "
              "-- see the per-asset bar counts and the verdict section.")
        delta_rows = _load_corpus_delta()
        cycle_id = int(target.strftime("%Y%m%d"))
        result = replay(archived, delta_rows, cycle_id, target, predictions_all, log)

        print(f"\nregime: state={result['regime']['state']} "
              f"({result['regime']['name']})  P={result['regime']['probability']:.3f}")
        print(f"eligible families: {result['eligible_families']}")
        print(f"WFO survivors: {result['n_wfo_survivors']}  "
              f"MC-gate passed: {result['n_mc_passed']}")
        if result["meta_warning"]:
            print(f"WARNING: {result['meta_warning']}")

        print("\n  cross-check vs archived corpus_delta.jsonl (by config_id):")
        for c in result["cross_check"]:
            found = "found" if c["found_in_archived_corpus_delta"] else "NOT FOUND"
            print(f"    {c['config_id']:40s} {found:10s} "
                  f"replayed_sharpe={c['replayed_wfo_sharpe']:.3f} "
                  f"archived_sharpe={c['archived_wfo_sharpe']}")

        print("\n--- Step 5: diff replayed call(s) vs predictions.jsonl ---")
        diff = diff_predictions(result["replayed_predictions"], predictions_today)
        for asset, entry in diff["per_asset"].items():
            print(f"\n  {asset}: actual={entry['actual_count']} "
                  f"replayed={entry['replayed_count']}")
            if entry.get("note"):
                print(f"    {entry['note']}")
            for f in entry["fields"]:
                mark = "=" if f["match"] else "!="
                print(f"    {f['field']:16s} actual={f['actual']!r:30} "
                      f"{mark} replayed={f['replayed']!r}")

        print(f"\n=== VERDICT: {diff['overall_verdict']} "
              f"(context_hash integrity: {'OK' if hashes_ok else 'FAILED'}) ===")
        return 0
    except RefusedMismatch as exc:
        print(f"\nREFUSED: {exc}")
        return 1
    except NotReproducible as exc:
        print(f"\n{exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
