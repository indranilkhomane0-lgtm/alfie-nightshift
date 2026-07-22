import json, logging, time
from datetime import date, datetime
from pathlib import Path
from typing import Optional
import numpy as np
import pandas as pd
import ccxt

from nightshift.config import (ASSETS, EXCHANGE_ID, OHLCV_DAYS,
    BRIEF_DIR, REGIME_NAMES, META_LIVE_WINDOW, OPTUNA_TRIALS)
from nightshift.db import (init_db, log_cycle_start, log_cycle_end,
    log_cycle_error, insert_config_entry, corpus_size)
from nightshift.regime_engine import RegimeEngine, compute_hmm_features
from nightshift.wfo_engine import WFOEngine
from nightshift.mc_gate import run_mc_gates
from nightshift.meta_model import get_meta_model
from nightshift.live_monitor import LiveMonitor
from nightshift.strategies import STRATEGY_REGISTRY
from nightshift.derivatives import get_all_signals

log = logging.getLogger(__name__)

# ── Real Binance OHLCV fetcher ────────────────────────────────────────────────

def load_price_data(asset: str, days: int = OHLCV_DAYS) -> pd.DataFrame:
    try:
        exchange = ccxt.binance({"enableRateLimit": True})
        since    = exchange.milliseconds() - days * 24 * 60 * 60 * 1000
        ohlcv    = exchange.fetch_ohlcv(asset, "1d", since=since, limit=days)
        df       = pd.DataFrame(ohlcv,
                       columns=["timestamp","open","high","low","close","volume"])
        df.index = pd.to_datetime(df["timestamp"], unit="ms")
        df       = df[["open","high","low","close","volume"]].astype(float)
        log.info("Fetched %d bars for %s from Binance", len(df), asset)
        return df
    except Exception as e:
        log.warning("Binance fetch failed for %s: %s — using synthetic fallback", asset, e)
        return _synthetic(asset, days)

def _synthetic(asset: str, days: int) -> pd.DataFrame:
    np.random.seed(hash(asset) % 2**31)
    ret   = np.random.normal(0.001, 0.03, days)
    close = 100 * np.cumprod(1 + ret)
    dates = pd.date_range(end=date.today(), periods=days, freq="D")
    return pd.DataFrame({"open":close*0.999,"high":close*1.01,
                         "low":close*0.99,"close":close,
                         "volume":np.abs(np.random.normal(1e8,2e7,days))},index=dates)

def load_derivatives_signals(asset: str) -> dict:
    """Placeholder — wire in Coinalyze/Coinglass API here later."""
    return {"funding_rate":0.01,"oi_trend_7d":0.02,"exchange_flow_7d":-0.01,
            "longshort_ratio":0.52,"btc_dominance_delta":0.0}

# ── Brief formatter ───────────────────────────────────────────────────────────

def _brief(cycle_id, regime, top_configs, monitor_statuses, meta_scores,
           corpus_n, duration):
    now   = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    lines = [
        "="*68,
        "  ALFIE — NIGHT SHIFT MORNING BRIEF",
        f"  Cycle #{cycle_id}  ·  {now}  ·  {duration:.0f}s",
        "="*68, "",
        "── REGIME ─────────────────────────────────────────────────────────",
        f"  {regime.state} — {regime.name.upper()}  "
        f"P={regime.probability:.0%}  days_in={regime.days_in_state}  "
        f"trans_p={regime.transition_p:.0%}",
        f"  Size mult: {regime.size_mult:.0%}  Vol target: {regime.vol_target:.0%} ann",
        f"  Eligible: {[k for k,v in regime.eligible_families.items() if v]}",
        "",
        "── META-MODEL ──────────────────────────────────────────────────────",
        f"  Corpus: {corpus_n} labelled rows  "
        f"{'ACTIVE' if corpus_n>=30 else 'STANDBY — need 30 rows'}",
        "",
        "── TOP CONFIGURATIONS ──────────────────────────────────────────────",
    ]
    if not top_configs:
        lines.append("  No configs passed all gates tonight.")
    for i,(cfg,ms) in enumerate(zip(top_configs,meta_scores),1):
        mc = cfg.mc_results
        lines += ["",
            f"  [{i}] {cfg.config_id}",
            f"      WFO  Sharpe={cfg.sharpe_oos:.2f}  Sortino={cfg.sortino_oos:.2f}"
            f"  MaxDD={cfg.max_dd_oos:.1%}",
            f"      GT={cfg.gt_score_oos:.3f}  stability_σ={cfg.param_stability:.3f}",
            f"      MC   G1={mc.get('gate1_p10',0):.2f}"
            f" G2={mc.get('gate2_p10',0):.2f}"
            f" G3={mc.get('gate3_p10',0):.2f}"
            f" G4={mc.get('gate4_p10',0):.2f}"
            f" G5_sens={mc.get('gate5_sensitivity',0):.2f}",
            f"      META {ms.explain()}",
            f"      Params: {json.dumps(cfg.params)}",
        ]
    lines += ["",
        "── LIVE HEALTH ─────────────────────────────────────────────────────",
    ]
    if not monitor_statuses:
        lines.append("  No strategies currently deployed.")
    for ms in monitor_statuses:
        if ms:
            lines.append(f"  {ms.emoji} {ms.config_id}: {ms.status.upper()} — {ms.message}")
    lines += ["",
        "── ACTION REQUIRED ─────────────────────────────────────────────────",
        "  Review top config above. Confirm or reject before any live change.",
        "  System is PAPER-ONLY. No positions executed without your approval.",
        "","="*68,
        "  ⚠  NOT FINANCIAL ADVICE. Research and educational use only.",
        "="*68,
    ]
    return "\n".join(lines)

# ── Main cycle ────────────────────────────────────────────────────────────────

class NightShiftCycle:
    def __init__(self, cycle_id=None, live_monitor=None):
        self.cycle_id   = cycle_id or int(date.today().strftime("%Y%m%d"))
        self.monitor    = live_monitor or LiveMonitor()
        self.regime_eng = RegimeEngine()
        self.meta       = get_meta_model()

    def run(self):
        t0 = time.time()
        init_db()
        log_cycle_start(self.cycle_id, str(date.today()))
        log.info("▶ Night shift cycle %d", self.cycle_id)
        try:
            path = self._stages(t0)
        except Exception as e:
            log.exception("Cycle failed: %s", e)
            log_cycle_error(self.cycle_id, str(e))
            raise
        log.info("✓ Cycle %d complete in %.0fs → %s",
                 self.cycle_id, time.time()-t0, path)
        return path

    def _stages(self, t0):
        # Stage 1 — ingest
        log.info("Stage 1/7: Fetching market data from Binance …")
        prices  = {a: load_price_data(a) for a in ASSETS}
        signals = {a: get_all_signals(a) for a in ASSETS}
        log.info("  %d assets loaded", len(ASSETS))

        # Stage 2 — collect pending outcomes
        log.info("Stage 2/7: Collecting pending live outcomes …")
        self.meta.collect_pending_outcomes(
            self.monitor.live_performance_snapshot(), {}, {})

        # Stage 3 — regime
        log.info("Stage 3/7: Regime classification …")
        btc  = prices["BTC/USDT"]
        feats= compute_hmm_features(btc)
        self.regime_eng.fit(feats)
        regime = self.regime_eng.classify(feats)
        log.info("  → %d — %s (P=%.2f, days_in=%d)",
                 regime.state, regime.name, regime.probability, regime.days_in_state)

        # Stage 4 — WFO
        log.info("Stage 4/7: Walk-forward optimisation …")
        eligible = [k for k,v in regime.eligible_families.items() if v]
        log.info("  Eligible: %s", eligible)
        all_results = []
        n_each = max(40, OPTUNA_TRIALS // max(len(eligible)*len(ASSETS),1))
        for family in eligible:
            spec = STRATEGY_REGISTRY.get(family)
            if not spec: continue
            for asset in ASSETS:
                eng = WFOEngine(spec["fn"], spec["param_space"], family, n_top=5)
                all_results.extend(eng.optimise(asset, prices[asset], n_each))
        log.info("  %d configs passed min-Sharpe", len(all_results))

        # Stage 5 — MC gating
        log.info("Stage 5/7: Monte Carlo gating …")
        mc_passed = []
        for res in all_results:
            strat_fn = STRATEGY_REGISTRY[res.strategy_family]["fn"]
            mc = run_mc_gates(res, strat_fn, prices[res.asset], n_sims=300)
            if mc.all_pass: mc_passed.append(res)
        log.info("  %d/%d passed all 5 gates", len(mc_passed), len(all_results))

        # Stage 6 — meta-model ranking
        log.info("Stage 6/7: Meta-model ranking …")
        if self.meta.should_retrain(self.cycle_id):
            ok = self.meta.train()
            if ok:
                log.info("  Retrained — AUC=%.3f R²=%.3f",
                         self.meta._cv_clf, self.meta._cv_reg)
        cfg_dicts = []
        for res in mc_passed:
            ds  = signals.get(res.asset, {})
            mc  = res.mc_results
            cfg_dicts.append({
                "config_id":res.config_id,"asset":res.asset,
                "strategy_family":res.strategy_family,"params":res.params,
                "deployment_date":str(date.today()),
                "wfo_sharpe":res.sharpe_oos,"wfo_sortino":res.sortino_oos,
                "wfo_calmar":res.calmar_oos,"wfo_max_dd":res.max_dd_oos,
                "wfo_win_rate":res.win_rate_oos,"wfo_gt_score":res.gt_score_oos,
                "wfo_n_trades":res.n_trades,
                "mc_gate1_p10":mc.get("gate1_p10",0),"mc_gate2_p10":mc.get("gate2_p10",0),
                "mc_gate3_p10":mc.get("gate3_p10",0),"mc_gate4_p10":mc.get("gate4_p10",0),
                "mc_gate5_sensitivity":mc.get("gate5_sensitivity",0),
                "mc_composite":mc.get("composite",0),
                "regime_state":regime.state,"regime_prob":regime.probability,
                "regime_days_in":regime.days_in_state,"regime_transition_p":regime.transition_p,
                "funding_rate":ds.get("funding_rate",0),"oi_trend_7d":ds.get("oi_trend_7d",0),
                "exchange_flow_7d":ds.get("exchange_flow_7d",0),
                "vol_ratio":regime.features_last.get("vol_ratio",1.0),
                "longshort_ratio":ds.get("longshort_ratio",0.5),
                "btc_dominance_delta":ds.get("btc_dominance_delta",0),
            })
        gt_scores   = [r.gt_score_oos for r in mc_passed]
        meta_scores = self.meta.rank(cfg_dicts, regime.state, gt_scores)
        order       = sorted(range(len(mc_passed)),
                             key=lambda i: meta_scores[i].rank_score, reverse=True)
        mc_passed   = [mc_passed[i] for i in order]
        meta_scores = [meta_scores[i] for i in order]
        cfg_dicts   = [cfg_dicts[i] for i in order]
        for cfg in cfg_dicts: insert_config_entry(self.cycle_id, cfg)
        top3  = mc_passed[:3]
        top3m = meta_scores[:3]
        log.info("  Ranked %d. Top: %s",
                 len(mc_passed), mc_passed[0].config_id if mc_passed else "none")

        # Stage 7 — monitor + brief
        log.info("Stage 7/7: Live monitor + brief …")
        monitor_statuses = self.monitor.check_all()
        for ms in monitor_statuses:
            if ms and ms.should_suspend:
                log.warning("AUTO-SUSPEND: %s", ms.config_id)
                self.monitor.deregister(ms.config_id)
        duration   = time.time() - t0
        brief_text = _brief(self.cycle_id, regime, top3, monitor_statuses,
                            top3m, corpus_size(), duration)
        brief_path = str(BRIEF_DIR / f"brief_{self.cycle_id}.txt")
        Path(brief_path).write_text(brief_text, encoding="utf-8")

        # --- prediction stamp: record a gradeable claim for the labeler ---
        try:
            from nightshift.stamp_prediction import stamp as _stamp
            if top3:
                _p = _stamp(cfg_dicts[0], prices[top3[0].asset]["close"].tolist())
                log.info("  Prediction stamped: %s %s @ %s",
                         _p["asset"], _p["direction"], _p["entry_price"])
        except Exception as _e:
            log.warning("prediction stamp failed (non-fatal): %s", _e)
        log_cycle_end(self.cycle_id, regime_state=regime.state,
            regime_name=regime.name, n_tested=len(all_results),
            n_passed=len(mc_passed),
            action=f"Top: {top3[0].config_id if top3 else 'none'}",
            brief_path=brief_path, duration=duration)
        return brief_path
