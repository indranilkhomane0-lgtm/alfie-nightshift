#!/usr/bin/env python3
"""
Tests for the rank()/cycle.py pairing fix and its detector.

MetaModel.rank() used to return bare scores already sorted; cycle.py then
re-sorted them by position as if they were still in input order, so the
reorder was the identity: the published #1 was MC-gate order and every
brief's META lines described other configs. rank() now returns
RankedConfig(index, score) and cycle._apply_ranking() reorders every
parallel list from the same element.

Also covers nightshift/self_audit.py's brief_ranking_findings() /
check_published_ranking_order(), including against the real published
brief_20261007.txt, which must be flagged.

Run directly:

    python3 tests/test_ranking_pairing.py
"""
import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from nightshift import self_audit  # noqa: E402

try:
    from nightshift import meta_model as mm  # noqa: E402
    from nightshift import cycle  # noqa: E402
    HAVE_DEPS = True
except ImportError:  # sklearn/pandas missing
    HAVE_DEPS = False

REGIME = NS(state=0, name="x", probability=1, days_in_state=0, transition_p=0,
            size_mult=1, vol_target=0.1, eligible_families={})


def _res(cid, gt):
    return NS(config_id=cid, sharpe_oos=1.0, sortino_oos=1.0, max_dd_oos=-0.01,
              gt_score_oos=gt, fold_sharpes=[], param_stability=999.0, params={},
              mc_results={})


@unittest.skipUnless(HAVE_DEPS, "sklearn/pandas not importable")
class RankPairingTest(unittest.TestCase):

    def setUp(self):
        p = mock.patch.object(mm, "regime_corpus_size", return_value=0)
        p.start()
        self.addCleanup(p.stop)
        # MC-gate order deliberately NOT score order.
        self.gts = [1.606, 7.413, 4.633, 5.977]
        self.res = [_res(f"c{i}", g) for i, g in enumerate(self.gts)]
        self.cfgs = [{"config_id": r.config_id} for r in self.res]

    def test_rank_returns_indices_into_input_best_first(self):
        ranked = mm.MetaModel().rank(self.cfgs, 0, self.gts)
        self.assertEqual([r.index for r in ranked], [1, 3, 2, 0])
        for r in ranked:
            self.assertEqual(r.score.config_id, self.cfgs[r.index]["config_id"])

    def test_ties_keep_input_order(self):
        ranked = mm.MetaModel().rank(self.cfgs[:3], 0, [2.0, 2.0, 2.0])
        self.assertEqual([r.index for r in ranked], [0, 1, 2])

    def test_apply_ranking_keeps_config_and_score_together(self):
        ranked = mm.MetaModel().rank(self.cfgs, 0, self.gts)
        res, cfgs, scores = cycle._apply_ranking(ranked, self.res, self.cfgs)
        self.assertEqual([r.config_id for r in res], ["c1", "c3", "c2", "c0"])
        for r, c, s in zip(res, cfgs, scores):
            self.assertEqual(r.config_id, s.config_id)
            self.assertEqual(c["config_id"], s.config_id)
            self.assertEqual(r.gt_score_oos, s.fallback_score)

    def test_brief_from_fixed_ranking_has_no_findings(self):
        ranked = mm.MetaModel().rank(self.cfgs, 0, self.gts)
        res, _, scores = cycle._apply_ranking(ranked, self.res, self.cfgs)
        text = cycle._brief(1, REGIME, res[:3], [], scores[:3], 0, 0)
        f = self_audit.brief_ranking_findings(text)
        self.assertEqual((f["identity"], f["order"], f["unlabelled"]), ([], [], []))
        self.assertIn("scored=c1", text)

    def test_old_pairing_is_caught_by_the_detector(self):
        """The defect, reproduced: bare sorted scores re-sorted by
        position -- identity order, mismatched META lines."""
        ranked = mm.MetaModel().rank(self.cfgs, 0, self.gts)
        old = [r.score for r in ranked]
        order = sorted(range(len(self.res)), key=lambda i: old[i].rank_score, reverse=True)
        self.assertEqual(order, [0, 1, 2, 3], "old reorder was the identity")
        text = cycle._brief(1, REGIME, [self.res[i] for i in order][:3], [],
                            [old[i] for i in order][:3], 0, 0)
        f = self_audit.brief_ranking_findings(text)
        self.assertTrue(f["identity"])


def _block(pos, cid, gt, meta, scored="__same__"):
    tail = "" if scored is None else f"  scored={cid if scored == '__same__' else scored}"
    return (f"\n  [{pos}] {cid}\n      WFO  Sharpe=1.00\n      GT={gt}  stability_σ=n/a\n"
            f"      META {meta}{tail}\n      Params: {{}}\n")


def _brief_text(*blocks):
    return ("── TOP CONFIGURATIONS ──\n" + "".join(blocks)
            + "\n── LIVE HEALTH ──\n  No strategies currently deployed.\n")


FB = "[FALLBACK] corpus too small — using GT-Score={}"
MT = "[META] P(survive)=0.50  pred_decay=1.00  uncertainty=±0.010  rank={}  (regime_n=0)"


class BriefRankingFindingsTest(unittest.TestCase):

    def test_consistent_fallback_brief_passes(self):
        t = _brief_text(_block(1, "a", "7.413", FB.format("7.413")),
                        _block(2, "b", "5.977", FB.format("5.977")))
        f = self_audit.brief_ranking_findings(t)
        self.assertEqual((f["identity"], f["order"], f["unlabelled"]), ([], [], []))

    def test_fallback_gt_mismatch_is_identity_finding(self):
        t = _brief_text(_block(1, "a", "1.606", FB.format("7.413"), scored=None))
        f = self_audit.brief_ranking_findings(t)
        self.assertEqual(len(f["identity"]), 1)
        self.assertEqual(f["unlabelled"], [1])

    def test_scored_id_mismatch_is_identity_finding(self):
        t = _brief_text(_block(1, "a", "1.0", MT.format("2.0000"), scored="b"))
        self.assertEqual(self_audit.brief_ranking_findings(t)["identity"][0]["scored"], "b")

    def test_scores_rising_down_the_list_is_order_finding(self):
        t = _brief_text(_block(1, "a", "1.0", MT.format("0.4655")),
                        _block(2, "b", "1.0", MT.format("1.3933")))
        self.assertEqual(len(self_audit.brief_ranking_findings(t)["order"]), 1)

    def test_real_published_brief_20261007_is_flagged(self):
        path = ROOT / "nightshift" / "briefs" / "brief_20261007.txt"
        if not path.exists():
            self.skipTest("brief_20261007.txt not in this checkout")
        f = self_audit.brief_ranking_findings(path.read_text())
        self.assertEqual(f["n_configs"], 3)
        self.assertTrue(any(x["pos"] == 1 and x["gt"] == "1.606"
                            and x["meta_gt_score"] == "7.413" for x in f["identity"]))


class PublishedRankingOrderCheckTest(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        p = mock.patch.object(self_audit, "BRIEF_DIR", self.dir)
        p.start()
        self.addCleanup(p.stop)
        self.addCleanup(self._tmp.cleanup)

    def _entry(self, name, text, sha=None):
        (self.dir / name).write_text(text, encoding="utf-8")
        return {"payload": {"type": "NIGHTLY_BRIEF", "brief_file": name,
                            "brief_sha256": sha or hashlib.sha256(text.encode()).hexdigest()}}

    MARKER = {"payload": {"type": "METHODOLOGY_CHANGE",
                          "data": {self_audit.RANKING_MARKER_KEY: {}}}}
    BAD = _brief_text(_block(1, "a", "1.606", FB.format("7.413"), scored=None))
    GOOD = _brief_text(_block(1, "a", "7.413", FB.format("7.413")))

    def test_skip_without_marker(self):
        r = self_audit.check_published_ranking_order([self._entry("b1.txt", self.BAD)])
        self.assertEqual(r["status"], "SKIP")

    def test_pre_marker_briefs_are_not_findings(self):
        entries = [self._entry("b1.txt", self.BAD), self.MARKER, self._entry("b2.txt", self.GOOD)]
        self.assertEqual(self_audit.check_published_ranking_order(entries)["status"], "PASS")

    def test_post_marker_mismatch_fails_and_counts(self):
        entries = [self.MARKER, self._entry("b2.txt", self.BAD)]
        r = self_audit.check_published_ranking_order(entries)
        self.assertEqual(r["status"], "FAIL")
        self.assertEqual(self_audit._finding_count(r), 2)  # identity + unlabelled

    def test_brief_not_matching_chained_sha_is_unverifiable(self):
        entries = [self.MARKER, self._entry("b2.txt", self.GOOD, sha="0" * 64)]
        r = self_audit.check_published_ranking_order(entries)
        self.assertEqual(r["status"], "FAIL")
        self.assertEqual(r["detail"]["briefs_unverifiable"][0]["reason"],
                         "sha256 differs from chain")


if __name__ == "__main__":
    unittest.main()
