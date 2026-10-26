#!/usr/bin/env python3
"""
Tests for the GT-Score published ranking with the meta-model in shadow,
and for core/reproduce.py's config_id-matched diff.

The published order and #1 come from GT-Score whatever the model says;
the trained model's ordering is recorded in the brief (a marked shadow
section) and in the chained NIGHTLY_BRIEF payload (payload["ranking"]),
never used for the published order.

Run directly:

    python3 tests/test_shadow_ranking.py
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "core"))

from nightshift import self_audit  # noqa: E402

try:
    from nightshift import meta_model as mm  # noqa: E402
    from nightshift import cycle  # noqa: E402
    HAVE_DEPS = True
except ImportError:
    HAVE_DEPS = False

REGIME = NS(state=0, name="x", probability=1, days_in_state=0, transition_p=0,
            size_mult=1, vol_target=0.1, eligible_families={})


def _res(cid, gt):
    return NS(config_id=cid, sharpe_oos=1.0, sortino_oos=1.0, max_dd_oos=-0.01,
              gt_score_oos=gt, fold_sharpes=[], param_stability=999.0, params={},
              mc_results={})


class _TrainedFake:
    """A 'trained' model whose ranking is the exact reverse of GT order."""
    _trained = True
    _n_train, _cv_clf, _cv_reg = 172, 0.552, -2.367

    def rank(self, configs, regime_state, gt_scores):
        n = len(configs)
        order = sorted(range(n), key=lambda i: gt_scores[i])  # ascending GT
        return [mm.RankedConfig(i, mm.MetaRankScore(
                    config_id=configs[i]["config_id"], rank_score=float(n - k),
                    survival_prob=0.9, predicted_decay=1.0, uncertainty=0.0,
                    regime_n_samples=0, model_active=True, fallback_score=gt_scores[i]))
                for k, i in enumerate(order)]


@unittest.skipUnless(HAVE_DEPS, "sklearn/pandas not importable")
class PublishedIsGtShadowIsModelTest(unittest.TestCase):

    def setUp(self):
        self.gts = [1.606, 7.413, 4.633, 5.977]
        self.res = [_res(f"c{i}", g) for i, g in enumerate(self.gts)]
        self.cfgs = [{"config_id": r.config_id} for r in self.res]

    def test_rank_by_gt_score_is_descending_and_stable(self):
        self.assertEqual([r.index for r in mm.rank_by_gt_score(self.cfgs, self.gts)],
                         [1, 3, 2, 0])
        self.assertEqual([r.index for r in mm.rank_by_gt_score(self.cfgs[:3], [1, 1, 1])],
                         [0, 1, 2])

    def test_trained_model_does_not_change_published_order(self):
        published, shadow = cycle._published_and_shadow(
            _TrainedFake(), self.cfgs, 0, self.gts)
        self.assertEqual([self.cfgs[r.index]["config_id"] for r in published],
                         ["c1", "c3", "c2", "c0"])
        self.assertEqual([cid for cid, _ in shadow], ["c0", "c2", "c3", "c1"])
        for cid, ms in shadow:
            self.assertEqual(cid, ms.config_id)

    def test_untrained_model_has_no_shadow(self):
        untrained = NS(_trained=False)
        published, shadow = cycle._published_and_shadow(untrained, self.cfgs, 0, self.gts)
        self.assertIsNone(shadow)
        self.assertIsNone(cycle._model_info(untrained))

    def _brief(self, meta):
        published, shadow = cycle._published_and_shadow(meta, self.cfgs, 0, self.gts)
        res, _, scores = cycle._apply_ranking(published, self.res, self.cfgs)
        text = cycle._brief(1, REGIME, res[:3], [], scores[:3], 31, 0,
                            shadow=shadow, model_info=cycle._model_info(meta))
        return text, scores, shadow

    def test_brief_labels_model_as_shadow_not_active(self):
        text, _, _ = self._brief(_TrainedFake())
        self.assertIn("SHADOW — NOT USED FOR THE PUBLISHED ORDER", text)
        self.assertIn("untrained-quality", text)
        self.assertIn("AUC=0.552", text)
        self.assertNotIn("ACTIVE", text)
        self.assertIn("published order: GT-Score", text)
        self.assertIn("shadow #1  c0", text)
        self.assertIn("Shadow #1 is published #1: no", text)

    def test_brief_passes_check_12_parser(self):
        text, _, _ = self._brief(_TrainedFake())
        f = self_audit.brief_ranking_findings(text)
        self.assertEqual(f["n_configs"], 3)
        self.assertEqual((f["identity"], f["order"], f["unlabelled"]), ([], [], []))

    def test_untrained_brief_has_no_shadow_section(self):
        text, _, _ = self._brief(NS(_trained=False))
        self.assertNotIn("SHADOW", text)
        self.assertIn("Untrained", text)

    def test_ranking_record_carries_both_orders_in_full(self):
        _, scores, shadow = self._brief(_TrainedFake())
        rec = cycle._ranking_record(scores, shadow, cycle._model_info(_TrainedFake()))
        self.assertEqual(rec["published_order"]["basis"], "gt_score")
        self.assertEqual([c["config_id"] for c in rec["published_order"]["configs"]],
                         ["c1", "c3", "c2", "c0"])
        self.assertFalse(rec["shadow_order"]["used_for_published_order"])
        self.assertEqual([c["config_id"] for c in rec["shadow_order"]["configs"]],
                         ["c0", "c2", "c3", "c1"])
        self.assertEqual(rec["shadow_order"]["model"]["cv_auc"], 0.552)
        json.dumps(rec)  # must be chainable


class PublishBriefChainsRankingTest(unittest.TestCase):

    def test_ranking_sidecar_lands_in_payload(self):
        from nightshift import publish_chain
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        brief = root / "brief_20990101.txt"
        brief.write_text("brief\n")
        rec = {"published_order": {"basis": "gt_score", "configs": []}, "shadow_order": None}
        brief.with_suffix(".ranking.json").write_text(json.dumps(rec))
        chain = root / "chain.jsonl"
        with mock.patch.object(publish_chain, "CHAIN_PATH", chain), \
             mock.patch.object(publish_chain, "_stamp_at_publish_time"), \
             mock.patch.object(sys, "argv", ["publish_chain.py", "--brief", str(brief)]):
            self.assertEqual(publish_chain.main() or 0, 0)
        payload = json.loads(chain.read_text().splitlines()[-1])["payload"]
        self.assertEqual(payload["ranking"], rec)
        self.assertFalse(os.stat(brief).st_mode & 0o222, "chained brief must be read-only")
        os.chmod(brief, 0o644)


class ReproduceDiffByConfigIdTest(unittest.TestCase):

    def setUp(self):
        import reproduce
        self.diff = reproduce.diff_predictions
        self.fields = reproduce.DIFF_FIELDS

    def _p(self, cid, asset="ETH/USDT", **kw):
        p = {f: 1 for f in self.fields}
        p.update({"asset": asset, "config_id": cid, "context_hash": "h"})
        p.update(kw)
        return p

    def test_same_calls_in_different_order_match(self):
        actual = [self._p("a"), self._p("b", entry_price=2)]
        replayed = [self._p("b", entry_price=2), self._p("a")]
        self.assertEqual(self.diff(replayed, actual)["overall_verdict"], "MATCH")

    def test_positional_pairing_would_have_compared_different_configs(self):
        """First-per-asset would pair a with b and report a field mismatch
        that doesn't exist; by config_id there is none."""
        actual = [self._p("a", entry_price=1), self._p("b", entry_price=2)]
        replayed = [self._p("b", entry_price=2), self._p("a", entry_price=1)]
        entry = self.diff(replayed, actual)["per_asset"]["ETH/USDT"]
        self.assertTrue(all(f["match"] for m in entry["matched"] for f in m["fields"]))

    def test_different_config_sets_mismatch_and_are_listed(self):
        report = self.diff([self._p("a"), self._p("c")], [self._p("a"), self._p("b")])
        entry = report["per_asset"]["ETH/USDT"]
        self.assertEqual(report["overall_verdict"], "MISMATCH")
        self.assertEqual(entry["only_actual"], ["b"])
        self.assertEqual(entry["only_replayed"], ["c"])

    def test_field_mismatch_on_matched_config_is_mismatch(self):
        report = self.diff([self._p("a", entry_price=2)], [self._p("a", entry_price=1)])
        self.assertEqual(report["overall_verdict"], "MISMATCH")

    def test_no_actual_is_mismatch(self):
        self.assertEqual(self.diff([self._p("a")], [])["overall_verdict"], "MISMATCH")


if __name__ == "__main__":
    unittest.main()
