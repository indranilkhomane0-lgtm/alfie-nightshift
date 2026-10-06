#!/usr/bin/env python3
"""
Tests for nightshift/archive_corpus.py -- the nightly corpus-delta
archiver (every mc_passed candidate the cycle inserted into corpus.db,
not just the published one, appended to reports/archive/corpus_delta.jsonl).

Builds a real throwaway SQLite DB with the same schema nightshift/db.py
creates (via its own init_db()/log_cycle_start()/log_cycle_end()/
insert_config_entry() -- dogfooding the real writer functions rather
than hand-rolling INSERT statements, so these tests break if the real
schema or those functions' contracts ever drift) and points
nightshift.db.DB_PATH at it. Never touches the real corpus.db.

stdlib only. Run directly:

    python3 tests/test_archive_corpus.py
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from nightshift import db as nsdb  # noqa: E402
from nightshift import archive_corpus  # noqa: E402

BASE_CYCLE_ID = archive_corpus.ARCHIVE_START_CYCLE_ID


def _cfg(asset="BTC/USDT", family="momentum", config_id="cfg1"):
    return {
        "config_id": config_id, "asset": asset, "strategy_family": family,
        "deployment_date": "2026-10-07", "wfo_sharpe": 1.1, "wfo_sortino": 1.2,
        "wfo_calmar": 1.3, "wfo_max_dd": -0.1, "wfo_win_rate": 0.5,
        "wfo_gt_score": 0.4, "wfo_n_trades": 10, "wfo_n_trials": 40,
        "wfo_n_candidates_surviving_min_sharpe": 5,
        "mc_gate1_p10": 0.1, "mc_gate2_p10": 0.1, "mc_gate3_p10": 0.1,
        "mc_gate4_p10": 0.1, "mc_gate5_sensitivity": 0.1, "mc_composite": 0.5,
        "regime_state": 1, "regime_prob": 0.8, "regime_days_in": 3,
        "regime_transition_p": 0.1, "funding_rate": 0.0001, "oi_trend_7d": 0.02,
        "exchange_flow_7d": -0.01, "vol_ratio": 1.0, "longshort_ratio": 0.5,
        "btc_dominance_delta": 0.001, "params": {"lookback": 14},
    }


class ArchiveCorpusTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.db_path = self.root / "corpus.db"
        self.archive_path = self.root / "archive" / "corpus_delta.jsonl"
        self.log_path = self.root / "archive_corpus.log"
        self._patches = [
            mock.patch.object(nsdb, "DB_PATH", self.db_path),
            mock.patch.object(archive_corpus, "ARCHIVE_PATH", self.archive_path),
            mock.patch.object(archive_corpus, "LOG_PATH", self.log_path),
        ]
        for p in self._patches:
            p.start()
        nsdb.init_db()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self._tmp.cleanup()

    def _lines(self):
        if not self.archive_path.exists():
            return []
        return [json.loads(l) for l in self.archive_path.read_text().splitlines() if l.strip()]

    def _make_complete_cycle(self, cycle_id, cfgs):
        nsdb.log_cycle_start(cycle_id, "2026-10-07")
        for cfg in cfgs:
            nsdb.insert_config_entry(cycle_id, cfg)
        nsdb.log_cycle_end(cycle_id, regime_state=1, regime_name="bull",
                            n_tested=len(cfgs) + 3, n_passed=len(cfgs),
                            action="test", brief_path="brief_test.txt", duration=1.0)

    def test_happy_path_archives_marker_and_all_candidates(self):
        self._make_complete_cycle(BASE_CYCLE_ID, [_cfg(config_id="a"), _cfg(config_id="b")])
        rc = archive_corpus.main()
        self.assertEqual(rc, 0)

        lines = self._lines()
        markers = [l for l in lines if l["kind"] == "cycle_marker"]
        candidates = [l for l in lines if l["kind"] == "candidate"]
        self.assertEqual(len(markers), 1)
        self.assertEqual(markers[0]["cycle_id"], BASE_CYCLE_ID)
        self.assertEqual(markers[0]["n_mc_passed"], 2)
        self.assertEqual(len(candidates), 2)
        self.assertEqual({c["config_id"] for c in candidates}, {"a", "b"})

    def test_zero_candidates_still_gets_a_marker(self):
        """A completed night where nothing passed MC gating must not be
        silently indistinguishable from 'not archived yet' -- it gets a
        marker with n_mc_passed=0 and zero candidate lines."""
        self._make_complete_cycle(BASE_CYCLE_ID, [])
        rc = archive_corpus.main()
        self.assertEqual(rc, 0)

        lines = self._lines()
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0]["kind"], "cycle_marker")
        self.assertEqual(lines[0]["n_mc_passed"], 0)

    def test_incomplete_cycle_is_not_archived(self):
        """A cycle that crashed (status stuck at 'running') has nothing
        meaningful in corpus.db for it and must not get a marker -- that
        night's failure is already recorded as its own PIPELINE_FAILURE
        chain entry; inventing a second record here would go stale."""
        nsdb.log_cycle_start(BASE_CYCLE_ID, "2026-10-07")  # never completed
        rc = archive_corpus.main()
        self.assertEqual(rc, 0)
        self.assertEqual(self._lines(), [])

    def test_cycles_before_archive_start_are_excluded_no_backfill(self):
        old_cycle_id = BASE_CYCLE_ID - 1
        self._make_complete_cycle(old_cycle_id, [_cfg()])
        rc = archive_corpus.main()
        self.assertEqual(rc, 0)
        self.assertEqual(self._lines(), [],
                          "a cycle before ARCHIVE_START_CYCLE_ID must never be backfilled")

    def test_idempotent_rerun_does_not_duplicate(self):
        self._make_complete_cycle(BASE_CYCLE_ID, [_cfg()])
        archive_corpus.main()
        first = self._lines()
        archive_corpus.main()
        second = self._lines()
        self.assertEqual(first, second, "a second run with nothing new must not re-append anything")

    def test_sweep_catches_up_multiple_missed_nights_in_one_pass(self):
        """The 'defer and sweep' requirement: if the archiver didn't run
        (or failed) for several nights in a row, the next successful run
        must catch up ALL of them, not just the most recent."""
        self._make_complete_cycle(BASE_CYCLE_ID, [_cfg(config_id="n1")])
        self._make_complete_cycle(BASE_CYCLE_ID + 1, [_cfg(config_id="n2")])
        self._make_complete_cycle(BASE_CYCLE_ID + 2, [])
        rc = archive_corpus.main()
        self.assertEqual(rc, 0)

        lines = self._lines()
        markers = sorted(l["cycle_id"] for l in lines if l["kind"] == "cycle_marker")
        self.assertEqual(markers, [BASE_CYCLE_ID, BASE_CYCLE_ID + 1, BASE_CYCLE_ID + 2])

    def test_write_failure_does_not_raise_and_is_caught_up_next_run(self):
        """Disk full / permission denied on the archive write itself:
        must not raise, must return 0 (never fail the night), and must
        leave the pending cycle uncaptured-but-not-lost -- the next,
        unmocked run picks it up, same shape as anchor_ots.py's defer."""
        self._make_complete_cycle(BASE_CYCLE_ID, [_cfg()])

        real_open = Path.open
        def failing_open(self_path, mode="r", *a, **kw):
            if self_path == archive_corpus.ARCHIVE_PATH and "a" in mode:
                raise OSError("No space left on device")
            return real_open(self_path, mode, *a, **kw)

        with mock.patch.object(Path, "open", failing_open):
            rc = archive_corpus.main()
        self.assertEqual(rc, 0, "a write failure must never fail the night")
        self.assertEqual(self._lines(), [], "nothing should have been written on failure")

        # Next run, disk is fine again -- the pending cycle is not lost.
        rc2 = archive_corpus.main()
        self.assertEqual(rc2, 0)
        markers = [l for l in self._lines() if l["kind"] == "cycle_marker"]
        self.assertEqual(len(markers), 1)
        self.assertEqual(markers[0]["cycle_id"], BASE_CYCLE_ID)

    def test_corrupt_existing_archive_does_not_crash(self):
        """A truncated or garbled archive file (crash mid-write, disk
        corruption) must degrade to 'treat as no cycles covered yet',
        not raise -- same fail-safe shape as every other reader here."""
        self.archive_path.parent.mkdir(parents=True, exist_ok=True)
        self.archive_path.write_text("{not valid json\n")
        self._make_complete_cycle(BASE_CYCLE_ID, [_cfg()])

        rc = archive_corpus.main()  # must not raise
        self.assertEqual(rc, 0)
        # The corrupt line is left as-is (append-only -- never rewritten);
        # new, valid lines are appended after it.
        text = self.archive_path.read_text()
        self.assertTrue(text.startswith("{not valid json"))
        self.assertIn('"kind":"cycle_marker"', text)


if __name__ == "__main__":
    unittest.main()
