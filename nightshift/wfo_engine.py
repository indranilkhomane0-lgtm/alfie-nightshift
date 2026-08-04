import hashlib, json, logging, warnings
from dataclasses import dataclass, field
import numpy as np
import pandas as pd
import optuna
from nightshift.config import (WFO_MIN_TRAIN_DAYS, WFO_TEST_DAYS, WFO_STEP_DAYS,
    WFO_EMBARGO_DAYS, OPTUNA_TRIALS, MIN_OOS_SHARPE, GT_W_SHARPE, GT_W_SIG,
    GT_W_CONSISTENCY, GT_W_SORTINO, GT_W_CALMAR)

optuna.logging.set_verbosity(optuna.logging.WARNING)
warnings.filterwarnings("ignore")
log = logging.getLogger(__name__)
ANN    = np.sqrt(252)
WARMUP = 300   # bars prepended to test slice so indicators can warm up

def _config_id(asset, family, params):
    """Content-addressed config id: the same (asset, family, params) always
    yields the same id, and different params never collide.

    The previous scheme was f"{asset}_{family}_{idx:04d}", where idx was the
    row's position in that night's results list -- positional, not
    identifying. Measured 2026-08-02: 29 config_ids appeared on more than one
    date and 28 of those carried DIFFERENT parameters. Anything reasoning
    about a config across nights (decay tracking, joining features to graded
    outcomes, LiveMonitor.register()) was silently comparing unrelated
    strategies that happened to land in the same list slot.

    Historic rows keep their positional ids -- nothing is rewritten. Joins
    must still be date-scoped to stay correct across the boundary.
    """
    canon = json.dumps({"asset": asset, "family": family, "params": params},
                       sort_keys=True, separators=(",", ":"))
    return f"{asset}_{family}_{hashlib.sha256(canon.encode()).hexdigest()[:12]}"


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
    n_trials_total: int
    n_candidates_surviving_min_sharpe: int
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

        # Walk-forward, not walk-then-test-everywhere. Each fold gets its
        # OWN Optuna study, scored only on prices.iloc[:train_end] where
        # train_end = ts - WFO_EMBARGO_DAYS -- strictly before that fold's
        # own test window, with a >=7-day embargo (WFO_EMBARGO_DAYS,
        # matching stamp_prediction.HOLD_DAYS) so the training tail isn't
        # immediately adjacent to a window a live system would still call
        # unsettled, and so indicator warm-up computed near the boundary
        # can't be read as having primed on the test window itself.
        #
        # WFO_STEP_DAYS == WFO_TEST_DAYS: folds tile [WFO_MIN_TRAIN_DAYS, n)
        # with zero overlap, so a given calendar bar belongs to exactly one
        # fold's test window. That's deliberate, not incidental -- an
        # earlier design used WFO_STEP_DAYS=14 (half of TEST_DAYS=28),
        # which covered the same unique OOS days but let two adjacent
        # folds' test windows share 14 of their 28 days, so a param set
        # rediscovered in both folds got that shared range pooled into its
        # oos_returns twice -- inflating sharpe_oos/n_trades sample size
        # without adding independent evidence. Non-overlapping folds make
        # that duplication structurally impossible rather than something
        # to dedupe or disclose after the fact. Trade-off: half as many
        # folds (11 vs 21 at n=500), so param_stability -- the std of
        # fold_sharpes for a param set that recurs across folds -- is now
        # computed over fewer observations per param set.
        #
        # Verified empirically: poisoning a single fold's own test window
        # never changes that fold's own selected params -- train_prices
        # below is a slice ending strictly before that fold's own ts, so
        # its objective function can never read that fold's held-out bars.
        #
        # The embargo costs the first fold outright: ts=WFO_MIN_TRAIN_DAYS
        # (180) minus 7 is 173, below WFO_MIN_TRAIN_DAYS, so it's skipped.
        param_oos = {}   # canonical params string -> accumulator, across the folds that (re)discovered it
        n_trials_total = 0

        for ts, te in folds:
            train_end = max(0, ts - WFO_EMBARGO_DAYS)
            if train_end < WFO_MIN_TRAIN_DAYS:
                continue
            train_prices = prices.iloc[:train_end]

            cache = {}
            def objective(trial):
                p  = self.param_space_fn(trial)
                ph = str(sorted(p.items()))
                if ph not in cache:
                    r = self.strategy_fn(train_prices, p)
                    cache[ph] = gt_score(r)
                return cache[ph]

            study = optuna.create_study(direction="maximize",
                        sampler=optuna.samplers.TPESampler(seed=42))
            study.optimize(objective, n_trials=tpf, show_progress_bar=False)
            n_trials_total += tpf

            seen = set()
            fold_top = []
            for t in sorted(study.trials, key=lambda x: -x.value):
                ph = str(sorted(t.params.items()))
                if ph not in seen:
                    seen.add(ph)
                    fold_top.append(t.params)
                if len(fold_top) >= self.n_top * 3:
                    break

            ctx_start  = max(0, ts - WARMUP)
            ctx_prices = prices.iloc[ctx_start:te]   # warmup + test bars
            n_test     = te - ts                       # true OOS length

            for params in fold_top:
                ph = str(sorted(params.items()))
                try:
                    full_r = self.strategy_fn(ctx_prices, params)
                    oos_r  = full_r[-n_test:]          # only the test portion
                    if len(oos_r) > 0:
                        acc = param_oos.setdefault(ph, {"params": params, "oos": [], "fgt": []})
                        acc["oos"].extend(oos_r.tolist())
                        acc["fgt"].append(gt_score(oos_r))
                except Exception:
                    pass

        # Build results, filter by min Sharpe
        results = []
        for ph, d in param_oos.items():
            oos = np.array(d["oos"])
            if len(oos) < 10: continue
            sh  = sharpe(oos)
            if sh < MIN_OOS_SHARPE: continue
            fs  = d["fgt"]
            results.append(WFOResult(
                config_id       = _config_id(asset, self.family, d["params"]),
                asset=asset, strategy_family=self.family, params=d["params"],
                gt_score_oos=gt_score(oos), sharpe_oos=sh,
                sortino_oos=sortino(oos), calmar_oos=calmar(oos),
                max_dd_oos=max_drawdown(oos), win_rate_oos=win_rate(oos),
                n_trades=int((oos!=0).sum()), oos_returns=oos,
                fold_sharpes=fs,
                param_stability=float(np.std(fs)) if len(fs)>1 else 999.0,
                n_trials_total=n_trials_total,
                n_candidates_surviving_min_sharpe=0))  # filled below

        # (b) multiplicity disclosure: every survivor carries the same two
        # numbers -- how many trials were actually searched this run, and
        # how many OTHER candidates also cleared MIN_OOS_SHARPE -- so a
        # reader can see "best of N" is not "the only one that worked."
        # No deflation of the Sharpe itself yet, just visibility.
        for r in results:
            r.n_candidates_surviving_min_sharpe = len(results)

        results.sort(key=lambda r: r.gt_score_oos, reverse=True)
        top = results[:self.n_top]
        log.info("WFO done: %d survived (of %d trials total across %d folds) → top %d",
                 len(results), n_trials_total, len(folds), len(top))
        return top
