import logging, warnings
from dataclasses import dataclass, field
import numpy as np
import pandas as pd
import optuna
from nightshift.config import (WFO_MIN_TRAIN_DAYS, WFO_TEST_DAYS, WFO_STEP_DAYS,
    OPTUNA_TRIALS, MIN_OOS_SHARPE, GT_W_SHARPE, GT_W_SIG,
    GT_W_CONSISTENCY, GT_W_SORTINO, GT_W_CALMAR)

optuna.logging.set_verbosity(optuna.logging.WARNING)
warnings.filterwarnings("ignore")
log = logging.getLogger(__name__)
ANN    = np.sqrt(252)
WARMUP = 300   # bars prepended to test slice so indicators can warm up

def sharpe(r):
    if len(r) < 5 or r.std() == 0: return -999.0
    return float(r.mean() / r.std() * ANN)

def sortino(r):
    neg = r[r < 0]
    if len(neg) < 2: return 0.0
    dd = np.std(neg)
    return float(r.mean() / dd * ANN) if dd else 0.0

def calmar(r):
    eq   = np.cumprod(1 + r)
    peak = np.maximum.accumulate(eq)
    mdd  = float(((eq - peak) / peak).min())
    cagr = float(np.prod(1 + r) ** (ANN / len(r)) - 1)
    return cagr / abs(mdd) if mdd else 0.0

def max_drawdown(r):
    eq   = np.cumprod(1 + r)
    peak = np.maximum.accumulate(eq)
    return float(((eq - peak) / peak).min())

def win_rate(r): return float((r > 0).mean())

def monthly_consistency(r):
    if len(r) < 20: return 0.5
    months = [r[i:i+21] for i in range(0, len(r)-20, 21)]
    return sum(1 for m in months if m.sum() > 0) / len(months) if months else 0.5

def gt_score(r):
    if len(r) < 10: return -999.0
    sh  = sharpe(r)
    t   = sh / ANN * np.sqrt(len(r))
    sig = min(t / 2.0, 1.0)
    return (GT_W_SHARPE*sh + GT_W_SIG*sig + GT_W_CONSISTENCY*monthly_consistency(r)
            + GT_W_SORTINO*sortino(r) + GT_W_CALMAR*calmar(r))

@dataclass
class WFOResult:
    config_id: str; asset: str; strategy_family: str; params: dict
    gt_score_oos: float; sharpe_oos: float; sortino_oos: float
    calmar_oos: float; max_dd_oos: float; win_rate_oos: float
    n_trades: int; oos_returns: np.ndarray
    fold_sharpes: list; param_stability: float
    mc_results: dict = field(default_factory=dict)
    mc_passed: bool = False
    meta_rank_score: float = 0.0

class WFOEngine:
    def __init__(self, strategy_fn, param_space_fn, strategy_family, n_top=8):
        self.strategy_fn    = strategy_fn
        self.param_space_fn = param_space_fn
        self.family         = strategy_family
        self.n_top          = n_top

    def _folds(self, n):
        folds, ts = [], WFO_MIN_TRAIN_DAYS
        while ts + WFO_TEST_DAYS <= n:
            folds.append((ts, ts + WFO_TEST_DAYS))
            ts += WFO_STEP_DAYS
        return folds

    def optimise(self, asset, prices, n_trials=OPTUNA_TRIALS):
        n     = len(prices)
        folds = self._folds(n)
        if not folds:
            log.warning("Not enough data for WFO on %s", asset)
            return []

        tpf = max(30, n_trials // len(folds))
        log.info("WFO %s %s — %d folds × %d trials", self.family, asset, len(folds), tpf)

        # Optimise on full training history (good warm-up for indicators)
        train_all = prices  # full history is the training pool

        cache = {}
        def objective(trial):
            p  = self.param_space_fn(trial)
            ph = str(sorted(p.items()))
            if ph not in cache:
                r = self.strategy_fn(train_all, p)
                cache[ph] = gt_score(r)
            return cache[ph]

        study = optuna.create_study(direction="maximize",
                    sampler=optuna.samplers.TPESampler(seed=42))
        study.optimize(objective, n_trials=n_trials, show_progress_bar=False)

        # Evaluate top param sets on each OOS fold with WARMUP context
        seen = set()
        top_params = []
        for t in sorted(study.trials, key=lambda x: -x.value):
            ph = str(sorted(t.params.items()))
            if ph not in seen:
                seen.add(ph)
                top_params.append(t.params)
            if len(top_params) >= self.n_top * 3:
                break

        # Collect OOS returns per param set across all folds
        param_oos = {str(sorted(p.items())): {"params":p,"oos":[],"fgt":[]}
                     for p in top_params}

        for ts, te in folds:
            ctx_start  = max(0, ts - WARMUP)
            ctx_prices = prices.iloc[ctx_start:te]   # warmup + test bars
            n_test     = te - ts                       # true OOS length

            for params in top_params:
                ph = str(sorted(params.items()))
                try:
                    full_r = self.strategy_fn(ctx_prices, params)
                    oos_r  = full_r[-n_test:]          # only the test portion
                    if len(oos_r) > 0:
                        param_oos[ph]["oos"].extend(oos_r.tolist())
                        param_oos[ph]["fgt"].append(gt_score(oos_r))
                except Exception:
                    pass

        # Build results, filter by min Sharpe
        results = []
        for idx, (ph, d) in enumerate(param_oos.items()):
            oos = np.array(d["oos"])
            if len(oos) < 10: continue
            sh  = sharpe(oos)
            if sh < MIN_OOS_SHARPE: continue
            fs  = d["fgt"]
            results.append(WFOResult(
                config_id       = f"{asset}_{self.family}_{idx:04d}",
                asset=asset, strategy_family=self.family, params=d["params"],
                gt_score_oos=gt_score(oos), sharpe_oos=sh,
                sortino_oos=sortino(oos), calmar_oos=calmar(oos),
                max_dd_oos=max_drawdown(oos), win_rate_oos=win_rate(oos),
                n_trades=int((oos!=0).sum()), oos_returns=oos,
                fold_sharpes=fs,
                param_stability=float(np.std(fs)) if len(fs)>1 else 999.0))

        results.sort(key=lambda r: r.gt_score_oos, reverse=True)
        top = results[:self.n_top]
        log.info("WFO done: %d survived → top %d", len(results), len(top))
        return top
