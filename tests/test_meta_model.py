#!/usr/bin/env python3
"""
Tests for nightshift/meta_model.py's should_retrain() gate fix and the
nightshift/cycle.py Stage-6 graduation-sidecar pattern it feeds.

Covers the structural defect disclosed on the chain: should_retrain()'s
pre-training branch used to gate on labeled_date_count()/corpus_size(),
both dependent on corpus.survived, which is written only via a call
chain ending at LiveMonitor.register() -- which has zero real callers,
making both counters permanently zero against real data. The fix
switches both branches to the prediction-graded source (
labeled_prediction_date_count() / get_prediction_labeled_corpus()) that
train() and _df() already use internally.

Builds a real throwaway corpus.db (via nightshift.db's own
init_db()/insert_config_entry(), not hand-rolled SQL) and a real
predictions.jsonl, so "graduation fires at 30" is checked against the
actual training/gating code, not a mocked stand-in for it.

stdlib + pandas/numpy/sklearn (same as the real pipeline) -- skips
cleanly if unavailable. Run directly:

    python3 tests/test_meta_model.py
"""
import json
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

try:
    import numpy as np  # noqa: F401
    import pandas as pd  # noqa: F401
    import sklearn  # noqa: F401
except ImportError:
    np = None

from nightshift import db as nsdb  # noqa: E402
from nightshift import meta_model as mm  # noqa: E402
from nightshift.meta_model import MetaModel, FEATURE_COLS  # noqa: E402


def _row(i, outcome, base=date(2026, 1, 1)):
    """One synthetic, internally-consistent (corpus row, predictions.jsonl
    row) pair for distinct date i -- config_id/deployment_date/cycle_date/
    settle_date all agree, exactly like the real join key
    get_prediction_labeled_corpus() uses."""
    dep = (base + timedelta(days=i)).isoformat()
    settle = (base + timedelta(days=i + 7)).isoformat()
    config_id = f"BTC/USDT_momentum_{i:04d}"
    win = outcome == "WIN"
    cfg = {
        "config_id": config_id, "asset": "BTC/USDT", "strategy_family": "momentum",
        "deployment_date": dep, "params": {"lookback": 14},
        "wfo_sharpe": 1.0 + (0.3 if win else -0.1), "wfo_sortino": 1.2, "wfo_calmar": 0.8,
        "wfo_max_dd": -0.1, "wfo_win_rate": 0.55, "wfo_gt_score": 0.4,
        "wfo_n_trades": 10, "wfo_n_trials": 40, "wfo_n_candidates_surviving_min_sharpe": 5,
        "mc_gate1_p10": 0.1, "mc_gate2_p10": 0.1, "mc_gate3_p10": 0.1,
        "mc_gate4_p10": 0.1, "mc_gate5_sensitivity": 0.1, "mc_composite": 0.5,
        "regime_state": 1, "regime_prob": 0.8, "regime_days_in": 3,
        "regime_transition_p": 0.1, "funding_rate": 0.0001, "oi_trend_7d": 0.02,
        "exchange_flow_7d": -0.01, "vol_ratio": 1.0, "longshort_ratio": 0.5,
        "btc_dominance_delta": 0.001,
    }
    pred = {
        "config_id": config_id, "cycle_date": dep, "asset": "BTC/USDT",
        "settle_date": settle, "outcome": outcome,
        "return_pct": 2.0 if win else -1.5,
    }
    return cfg, pred


@unittest.skipIf(np is None, "pandas/numpy/sklearn not importable in this environment")
class ShouldRetrainGateTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.db_path = root / "corpus.db"
        self.pred_path = root / "predictions.jsonl"
        self.pred_path.write_text("")
        self._patches = [
            mock.patch.object(nsdb, "DB_PATH", self.db_path),
            mock.patch.object(nsdb, "PREDICTIONS_PATH", self.pred_path),
        ]
        for p in self._patches:
            p.start()
        nsdb.init_db()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self._tmp.cleanup()

    def _seed(self, n_dates, outcomes=None):
        """Seeds n_dates distinct (corpus row, graded prediction) pairs.
        outcomes: list of "WIN"/"LOSS" per date, defaults to an
        alternating mix (so the >=2-class check passes)."""
        if outcomes is None:
            outcomes = ["WIN" if i % 2 == 0 else "LOSS" for i in range(n_dates)]
        preds = []
        for i in range(n_dates):
            cfg, pred = _row(i, outcomes[i])
            nsdb.insert_config_entry(20260101 + i, cfg)
            preds.append(pred)
        with self.pred_path.open("w") as f:
            for p in preds:
                f.write(json.dumps(p) + "\n")

    def test_gate_closed_below_30_dates(self):
        self._seed(29)
        meta = MetaModel()
        self.assertFalse(meta.should_retrain(cycle_id=1))

    def test_gate_opens_at_exactly_30_dates(self):
        self._seed(30)
        meta = MetaModel()
        self.assertTrue(meta.should_retrain(cycle_id=1))

    def test_graduation_actually_trains_against_synthetic_corpus(self):
        """Not just that the gate opens -- that train() itself, called
        because the gate opened, actually succeeds and flips _trained."""
        self._seed(30)
        meta = MetaModel()
        self.assertTrue(meta.should_retrain(cycle_id=1))
        was_trained = meta._trained
        ok = meta.train()
        self.assertTrue(ok)
        self.assertFalse(was_trained)
        self.assertTrue(meta._trained)
        self.assertEqual(meta._n_train, 30)

    def test_gate_no_longer_depends_on_dead_corpus_survived_path(self):
        """The actual regression test: labeled_date_count()/corpus_size()
        both read corpus.survived, which nothing in this fixture ever
        sets (insert_config_entry() never touches it, matching the real
        nightly path -- LiveMonitor.register() is never called). If the
        gate still depended on that path, it would stay closed here
        despite 30 real, graded dates existing."""
        self._seed(30)
        self.assertEqual(nsdb.labeled_date_count(), 0,
                          "fixture must not accidentally set survived")
        self.assertEqual(nsdb.corpus_size(), 0)
        meta = MetaModel()
        self.assertTrue(meta.should_retrain(cycle_id=1),
                         "gate must open from the prediction-graded path alone")

    def test_post_training_growth_trigger_uses_prediction_based_source(self):
        """The second half of the fix: after training, should_retrain()'s
        growth check must also read the live, prediction-based count
        (via get_prediction_labeled_corpus()), not corpus_size() (also
        permanently zero). Seed 30, train, seed 6 more (20% of 30) with
        a cycle_id that does NOT land on the META_RETRAIN_EVERY cadence,
        and confirm growth alone triggers it."""
        self._seed(30)
        meta = MetaModel()
        meta.train()
        self.assertEqual(meta._n_train, 30)

        self._seed(36)  # +6 rows = +20% growth over the trained 30
        from nightshift.config import META_RETRAIN_EVERY
        non_cadence_cycle_id = META_RETRAIN_EVERY + 1  # not a multiple of it
        self.assertNotEqual(non_cadence_cycle_id % META_RETRAIN_EVERY, 0)
        self.assertTrue(meta.should_retrain(non_cadence_cycle_id),
                         "20% growth via the prediction-based count must trigger a retrain "
                         "even off the fixed cadence")

    def test_failed_train_leaves_model_in_fallback_not_half_graduated(self):
        """A corpus that clears the date-count gate but fails train()'s
        OWN internal data-quality gate (here: only one outcome class --
        every date is a WIN) must leave _trained False -- fallback,
        not some half-trained state -- and rank() must keep using the
        GT-score fallback path."""
        self._seed(30, outcomes=["WIN"] * 30)
        meta = MetaModel()
        self.assertTrue(meta.should_retrain(cycle_id=1),
                         "the date-count gate alone doesn't check class balance")
        was_trained = meta._trained
        ok = meta.train()
        self.assertFalse(ok, "train() must refuse a single-class corpus")
        self.assertFalse(was_trained)
        self.assertFalse(meta._trained, "must remain in fallback, not half-graduated")

        scores = meta.rank([{"config_id": "x"}], regime_state=1, gt_scores=[0.42])
        self.assertFalse(scores[0].model_active)
        self.assertEqual(scores[0].rank_score, 0.42)

    def test_transition_only_sidecar_pattern_does_not_refire_on_retrain(self):
        """Reproduces cycle.py Stage 6's exact guard
        (`if ok and not was_trained`) against a real MetaModel across
        two simulated nights: night 1 graduates and would write the
        sidecar; night 2 is a routine retrain (already trained) and
        must NOT re-enter that branch, even though train() succeeds
        again and should_retrain() fires again."""
        from nightshift.cycle import _write_graduation_sidecar
        self._seed(30)
        meta = MetaModel()

        with tempfile.TemporaryDirectory() as sidecar_tmp:
            sidecar_path = Path(sidecar_tmp) / "meta_model_graduation.json"
            with mock.patch("nightshift.cycle.GRADUATION_SIDECAR_PATH", sidecar_path):
                # Night 1: graduation.
                self.assertTrue(meta.should_retrain(cycle_id=1))
                was_trained = meta._trained
                ok = meta.train()
                if ok and not was_trained:
                    _write_graduation_sidecar(1, meta)
                self.assertTrue(sidecar_path.exists(), "night 1 must write the sidecar")
                first_write_content = sidecar_path.read_text()
                sidecar_path.unlink()  # simulate publish_chain.py --graduation consuming it

                # Night 2: more data arrives, a routine retrain fires
                # (growth trigger), but the model was ALREADY trained
                # going in -- must not write a second graduation sidecar.
                self._seed(36)
                from nightshift.config import META_RETRAIN_EVERY
                non_cadence_cycle_id = META_RETRAIN_EVERY + 1
                self.assertTrue(meta.should_retrain(non_cadence_cycle_id))
                was_trained = meta._trained
                self.assertTrue(was_trained, "night 2 must start already-trained")
                ok = meta.train()
                self.assertTrue(ok, "a routine retrain should still succeed")
                if ok and not was_trained:
                    _write_graduation_sidecar(non_cadence_cycle_id, meta)
                self.assertFalse(sidecar_path.exists(),
                                  "a routine retrain must never re-write the graduation sidecar")
        self.assertIn('"first_training":true', first_write_content.replace(" ", ""))


if __name__ == "__main__":
    unittest.main()
