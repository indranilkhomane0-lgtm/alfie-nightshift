import logging, pickle, warnings
from dataclasses import dataclass
from pathlib import Path
from datetime import date, timedelta
import numpy as np
from sklearn.ensemble import GradientBoostingClassifier, GradientBoostingRegressor
from sklearn.preprocessing import StandardScaler
from sklearn.pipeline import Pipeline
from sklearn.model_selection import cross_val_score
from nightshift.config import (META_MIN_SAMPLES, META_SURVIVE_THRESHOLD,
    META_LIVE_WINDOW, META_RETRAIN_EVERY, META_N_ESTIMATORS,
    META_MAX_DEPTH, META_LEARNING_RATE, META_SEED, ROOT)
from nightshift.db import get_labelled_corpus, corpus_size, regime_corpus_size

log = logging.getLogger(__name__)
warnings.filterwarnings("ignore")
MODEL_PATH = ROOT / "meta_model.pkl"

FEATURE_COLS = [
    "wfo_sharpe","wfo_sortino","wfo_calmar","wfo_max_dd","wfo_win_rate","wfo_gt_score",
    "mc_gate1_p10","mc_gate2_p10","mc_gate3_p10","mc_gate4_p10","mc_gate5_sensitivity","mc_composite",
    "regime_state","regime_prob","regime_days_in","regime_transition_p",
    "funding_rate","oi_trend_7d","exchange_flow_7d","vol_ratio","longshort_ratio","btc_dominance_delta",
]

@dataclass
class MetaRankScore:
    config_id: str; rank_score: float; survival_prob: float
    predicted_decay: float; uncertainty: float; regime_n_samples: int
    model_active: bool; fallback_score: float

    def explain(self):
        if not self.model_active:
            return f"[FALLBACK] corpus too small — using GT-Score={self.fallback_score:.3f}"
        return (f"[META] P(survive)={self.survival_prob:.2f}  "
                f"pred_decay={self.predicted_decay:.2f}  "
                f"uncertainty=±{self.uncertainty:.3f}  "
                f"rank={self.rank_score:.4f}  (regime_n={self.regime_n_samples})")

class MetaModel:
    def __init__(self):
        self._clf = None; self._reg = None
        self._trained = False; self._n_train = 0
        self._cv_clf = 0.0; self._cv_reg = 0.0

    def _pipe(self, est):
        return Pipeline([("scaler", StandardScaler()), ("model", est)])

    def _df(self):
        import pandas as pd
        rows = get_labelled_corpus()
        if not rows: return pd.DataFrame()
        df = pd.DataFrame(rows).dropna(subset=FEATURE_COLS+["survived","decay_ratio"])
        return df

    def train(self, force=False):
        import pandas as pd
        n = corpus_size()
        if n < META_MIN_SAMPLES and not force:
            log.info("Corpus %d/%d — meta-model standby", n, META_MIN_SAMPLES)
            return False
        df = self._df()
        if len(df) < META_MIN_SAMPLES and not force: return False
        X     = df[FEATURE_COLS].values.astype(float)
        y_clf = df["survived"].values.astype(int)
        y_reg = df["decay_ratio"].values.astype(float)
        msl   = max(2, len(df)//50)
        clf = GradientBoostingClassifier(n_estimators=META_N_ESTIMATORS,
            max_depth=META_MAX_DEPTH, learning_rate=META_LEARNING_RATE,
            subsample=0.8, min_samples_leaf=msl, random_state=META_SEED,
            validation_fraction=0.15, n_iter_no_change=20)
        self._clf = self._pipe(clf); self._clf.fit(X, y_clf)
        cv = min(5, len(df)//10)
        self._cv_clf = float(cross_val_score(self._clf, X, y_clf,
                             cv=cv, scoring="roc_auc", error_score=0.0).mean())
        reg = GradientBoostingRegressor(n_estimators=META_N_ESTIMATORS,
            max_depth=META_MAX_DEPTH, learning_rate=META_LEARNING_RATE,
            subsample=0.8, min_samples_leaf=msl, loss="huber",
            random_state=META_SEED, validation_fraction=0.15, n_iter_no_change=20)
        self._reg = self._pipe(reg); self._reg.fit(X, y_reg)
        self._cv_reg = float(cross_val_score(self._reg, X, y_reg,
                             cv=cv, scoring="r2", error_score=0.0).mean())
        self._trained = True; self._n_train = len(df)
        log.info("Meta-model trained: n=%d  AUC=%.3f  R²=%.3f",
                 self._n_train, self._cv_clf, self._cv_reg)
        self._save(); return True

    def _feat(self, d):
        return np.array([float(d.get(c, 0.0)) for c in FEATURE_COLS]).reshape(1,-1)

    def rank(self, configs, regime_state, gt_scores):
        rn  = regime_corpus_size(regime_state)
        out = []
        for cfg, gt in zip(configs, gt_scores):
            cid = cfg.get("config_id","?")
            if not self._trained:
                out.append(MetaRankScore(config_id=cid, rank_score=gt,
                    survival_prob=0.5, predicted_decay=0.5, uncertainty=1.0,
                    regime_n_samples=rn, model_active=False, fallback_score=gt))
                continue
            X = self._feat(cfg)
            try:
                sp  = float(self._clf.predict_proba(X)[0,1])
                pd_ = float(self._reg.predict(X)[0])
                try:
                    scaler = self._reg.named_steps["scaler"]
                    Xt     = scaler.transform(X)
                    stages = list(self._reg.named_steps["model"].staged_predict(Xt))
                    unc    = float(np.std([float(s[0]) for s in stages[-50:]]))
                except: unc = 0.0
                rs  = pd_ * sp * (1.0 - min(unc, 0.5))
                out.append(MetaRankScore(config_id=cid, rank_score=rs,
                    survival_prob=sp, predicted_decay=pd_, uncertainty=unc,
                    regime_n_samples=rn, model_active=True, fallback_score=gt))
            except Exception as e:
                log.warning("Meta inference error %s: %s", cid, e)
                out.append(MetaRankScore(config_id=cid, rank_score=gt,
                    survival_prob=0.5, predicted_decay=0.5, uncertainty=1.0,
                    regime_n_samples=rn, model_active=False, fallback_score=gt))
        out.sort(key=lambda s: s.rank_score, reverse=True)
        return out

    def feature_importances(self):
        if not self._trained: return {}
        imp = self._clf.named_steps["model"].feature_importances_
        return dict(sorted(zip(FEATURE_COLS, imp), key=lambda x: -x[1]))

    def top_features(self, n=8):
        return list(self.feature_importances().items())[:n]

    def corpus_health(self):
        import pandas as pd
        df = self._df()
        if df.empty: return {"status":"empty","n_labelled":0}
        return {"status":"active" if self._trained else "standby",
                "n_labelled":len(df),
                "n_needed":META_MIN_SAMPLES,
                "pct_complete":min(100, len(df)/META_MIN_SAMPLES*100),
                "survive_rate":float(df["survived"].mean()),
                "avg_decay_ratio":float(df["decay_ratio"].mean()),
                "cv_auc":self._cv_clf, "cv_r2":self._cv_reg,
                "model_trained_n":self._n_train}

    def should_retrain(self, cycle_id):
        if not self._trained: return corpus_size() >= META_MIN_SAMPLES
        growth = corpus_size() - self._n_train
        return (cycle_id % META_RETRAIN_EVERY == 0) or (growth >= self._n_train*0.20)

    def collect_pending_outcomes(self, live_performance, live_drawdowns, live_n_trades):
        from nightshift.db import get_pending_outcomes, update_live_outcome
        cutoff  = str(date.today() - timedelta(days=META_LIVE_WINDOW))
        pending = get_pending_outcomes(cutoff)
        updated = 0
        for row in pending:
            cid = row["config_id"]
            if cid not in live_performance: continue
            ls   = live_performance[cid]
            ldd  = live_drawdowns.get(cid, 0.0)
            nt   = live_n_trades.get(cid, 0)
            wsh  = row.get("wfo_sharpe") or 1.0
            dr   = ls / wsh if wsh else 0.0
            surv = dr >= META_SURVIVE_THRESHOLD
            update_live_outcome(cid, row["deployment_date"],
                                ls, ldd, nt, dr, surv, "auto-collected")
            updated += 1
        if updated: log.info("Collected %d outcomes → corpus %d", updated, corpus_size())
        return updated

    def _save(self):
        with open(MODEL_PATH, "wb") as f:
            pickle.dump({"clf":self._clf,"reg":self._reg,
                         "n_train":self._n_train,
                         "cv_clf":self._cv_clf,"cv_reg":self._cv_reg}, f)
        log.info("Meta-model saved → %s", MODEL_PATH)

    def load(self):
        if not MODEL_PATH.exists(): return False
        try:
            with open(MODEL_PATH,"rb") as f: d = pickle.load(f)
            self._clf=d["clf"]; self._reg=d["reg"]
            self._n_train=d.get("n_train",0)
            self._cv_clf=d.get("cv_clf",0.0); self._cv_reg=d.get("cv_reg",0.0)
            self._trained=True
            log.info("Meta-model loaded (n=%d)", self._n_train); return True
        except Exception as e:
            log.warning("Could not load meta-model: %s", e); return False

_mm = None
def get_meta_model():
    global _mm
    if _mm is None: _mm = MetaModel(); _mm.load()
    return _mm
