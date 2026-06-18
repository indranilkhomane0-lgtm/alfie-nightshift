import logging, warnings
from dataclasses import dataclass
import numpy as np
import pandas as pd
from hmmlearn import hmm
from nightshift.config import (N_REGIMES, REGIME_TRAIN_DAYS, HMM_ITER,
    REGIME_SEED, REGIME_NAMES, REGIME_FAMILY_GATE, REGIME_SIZE_MULT, REGIME_VOL_TARGET)

log = logging.getLogger(__name__)
warnings.filterwarnings("ignore")

def hurst_exponent(ts):
    lags = range(2, min(20, len(ts)//2))
    tau  = [np.sqrt(np.std(np.subtract(ts[lag:], ts[:-lag]))) for lag in lags]
    if len(tau) < 2 or any(t == 0 for t in tau):
        return 0.5
    return float(np.polyfit(np.log(lags), np.log(tau), 1)[0])

def compute_hmm_features(prices, funding=None):
    close   = prices["close"]
    returns = close.pct_change()
    vol_10  = returns.rolling(10).std()
    vol_30  = returns.rolling(30).std()
    feats   = pd.DataFrame(index=prices.index)
    feats["ret_5d"]    = returns.rolling(5).mean()
    feats["ret_20d"]   = returns.rolling(20).mean()
    feats["vol_10d"]   = vol_10
    feats["vol_30d"]   = vol_30
    feats["vol_ratio"] = (vol_10 / vol_30.replace(0, np.nan)).fillna(1.0)
    feats["hurst"]     = returns.fillna(0).rolling(50).apply(
                             lambda x: hurst_exponent(x), raw=True)
    feats["funding"]   = (funding.rolling(7).mean() if funding is not None
                          else pd.Series(0.0, index=prices.index))
    return feats.dropna()

@dataclass
class RegimeResult:
    state: int; name: str; probability: float; posterior: np.ndarray
    days_in_state: int; transition_p: float; eligible_families: dict
    size_mult: float; vol_target: float; features_last: dict

class RegimeEngine:
    def __init__(self):
        self._model = None
        self._state_map = {}
        self._history = []

    def fit(self, features):
        X = features.iloc[-REGIME_TRAIN_DAYS:].values.astype(float)
        model = hmm.GaussianHMM(n_components=N_REGIMES, covariance_type="full",
                                 n_iter=HMM_ITER, random_state=REGIME_SEED, verbose=False)
        model.fit(X)
        raw = model.predict(X)
        mean_ret = {s: X[raw==s, 0].mean() for s in range(N_REGIMES)}
        sorted_s = sorted(mean_ret, key=lambda s: mean_ret[s])
        canonical = {3:0, 2:1, 1:2, 0:3}
        self._state_map = {sorted_s[r]: canonical[r] for r in range(N_REGIMES)}
        self._model  = model
        self._history = [self._state_map[s] for s in raw]
        log.info("HMM fitted. Distribution: %s",
                 {REGIME_NAMES[v]: int((np.array(self._history)==v).sum())
                  for v in range(N_REGIMES)})
        return self

    def classify(self, features):
        X          = features.values.astype(float)
        raw_states = self._model.predict(X)
        posteriors = self._model.predict_proba(X)
        raw_cur    = int(raw_states[-1])
        state      = self._state_map.get(raw_cur, raw_cur)
        post_raw   = posteriors[-1]
        post_canon = np.zeros(N_REGIMES)
        for raw, can in self._state_map.items():
            post_canon[can] = post_raw[raw]
        prob = float(post_canon[state])
        days_in = 1
        for s in reversed(self._history[:-1]):
            if s == state: days_in += 1
            else: break
        trans_p = 1.0 - float(self._model.transmat_[raw_cur, raw_cur])
        feat_last = dict(zip(features.columns, X[-1]))
        return RegimeResult(
            state=state, name=REGIME_NAMES[state], probability=prob,
            posterior=post_canon, days_in_state=days_in, transition_p=trans_p,
            eligible_families=REGIME_FAMILY_GATE[state],
            size_mult=REGIME_SIZE_MULT[state], vol_target=REGIME_VOL_TARGET[state],
            features_last=feat_last)
