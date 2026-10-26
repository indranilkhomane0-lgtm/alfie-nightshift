#!/usr/bin/env python3
"""
Tests that the real NightShiftCycle archives every asset's closed-bar
window in Stage 1 on a real fetch -- not only assets that later stamp a
prediction -- and never archives synthetic (test-mode) prices. Plus the
freeze-duration numbers core/verify_chain.py now reports.

Run directly:

    python3 tests/test_archive_every_asset.py
"""
import gzip
import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "core"))

from nightshift import archive_ohlcv, brief_guard  # noqa: E402


class _Stop(Exception):
    """Raised at Stage 2 so the test cycle ends right after Stage 1."""


class CycleArchivesEveryAssetTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        try:
            import pandas  # noqa: F401
            import sklearn  # noqa: F401
            import hmmlearn  # noqa: F401
        except ImportError as exc:
            raise unittest.SkipTest(f"full pipeline deps not importable: {exc}")

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.archive = root / "ohlcv.csv.gz"
        from nightshift import cycle, db as nsdb, meta_model
        self.cycle = cycle
        self._patches = [
            mock.patch.object(archive_ohlcv, "ARCHIVE_PATH", self.archive),
            mock.patch.object(archive_ohlcv, "LOG_PATH", root / "archive.log"),
            mock.patch.object(brief_guard, "CHAIN_PATH", root / "chain.jsonl"),
            mock.patch.object(nsdb, "DB_PATH", root / "corpus.db"),
            mock.patch.object(meta_model, "MODEL_PATH", root / "meta_model.pkl"),
            mock.patch.object(cycle, "get_all_signals_with_status",
                              return_value=({}, {})),
        ]
        for p in self._patches:
            p.start()
        archive_ohlcv._archived_this_run.clear()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self._tmp.cleanup()
        archive_ohlcv._archived_this_run.clear()

    def _exchange(self, fail=False):
        ex = mock.Mock()
        day = 86_400_000
        now = 1_790_000_000_000 // day * day
        ex.milliseconds.return_value = now
        if fail:
            ex.fetch_ohlcv.side_effect = Exception("simulated outage")
        else:
            ex.fetch_ohlcv.return_value = [
                [now - (60 - k) * day, 100 + k, 101 + k, 99 + k, 100.5 + k, 1000.0]
                for k in range(60)]
        return ex

    def _run_to_stage_2(self, night):
        with mock.patch.object(night.meta, "collect_pending_outcomes", side_effect=_Stop):
            with self.assertRaises(_Stop):
                night.run()

    def _archived_assets(self):
        if not self.archive.exists():
            return set()
        return {l.split(",")[1] for l in gzip.open(self.archive, "rt").read().splitlines() if l}

    def test_real_fetch_archives_all_three_assets_before_any_stamp(self):
        with mock.patch.object(self.cycle.ccxt, "binance", return_value=self._exchange()):
            self._run_to_stage_2(self.cycle.NightShiftCycle(cycle_id=99999903))
        self.assertEqual(self._archived_assets(), set(self.cycle.ASSETS))

    def test_synthetic_prices_are_never_archived(self):
        with mock.patch.object(self.cycle.ccxt, "binance",
                               return_value=self._exchange(fail=True)):
            self._run_to_stage_2(self.cycle.NightShiftCycle(
                cycle_id=99999904, allow_synthetic_prices=True))
        self.assertFalse(self.archive.exists())

    def test_stamp_after_stage_1_archive_is_a_no_op_with_the_same_window(self):
        """stamp() archives through the same per-run key; the window it
        hashes is the one Stage 1 already archived."""
        from nightshift import stamp_prediction
        from core.bar_calendar import is_utc_daily_bar_complete
        with mock.patch.object(self.cycle.ccxt, "binance", return_value=self._exchange()):
            df = self.cycle.load_price_data("BTC/USDT")
        closed = df[[is_utc_daily_bar_complete(i) for i in df.index]]
        from datetime import date
        archive_ohlcv.archive_closed_bars("BTC/USDT", closed, "binance", date.today().isoformat())
        before = hashlib.sha256(self.archive.read_bytes()).hexdigest()
        with mock.patch.object(stamp_prediction, "PRED_PATH", Path(self._tmp.name) / "p.jsonl"):
            stamp_prediction.stamp({"asset": "BTC/USDT", "config_id": "c", "strategy_family": "momentum",
                                    "params": {"fast_ma": 5, "slow_ma": 20, "rsi_entry": 10}},
                                   df, source="binance")
        self.assertEqual(hashlib.sha256(self.archive.read_bytes()).hexdigest(), before)


class FreezeDurationStatsTest(unittest.TestCase):

    def _chain(self, payloads, dates):
        import verify_chain
        prev = "0" * 64
        tmp = tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False)
        with tmp:
            for p, d in zip(payloads, dates):
                h = hashlib.sha256(prev.encode() + verify_chain.canonical(p)).hexdigest()
                tmp.write(json.dumps({"published_at_utc": d, "prev_hash": prev,
                                      "payload": p, "entry_hash": h}) + "\n")
                prev = h
        return verify_chain.compute_stats(Path(tmp.name), Path(tempfile.mkdtemp()))

    def test_current_version_since_and_nights_and_distinct_count(self):
        A, B = "a" * 64, "b" * 64
        stats = self._chain([
            {"type": "NIGHTLY_BRIEF", "strategy_version": A},
            {"type": "METHODOLOGY_CHANGE", "strategy_version": B},
            {"type": "NIGHTLY_BRIEF", "strategy_version": B},
            {"type": "AUDIT_RESULT", "strategy_version": B},
            {"type": "NIGHTLY_BRIEF", "strategy_version": B},
        ], ["2026-01-01T00:00:00+00:00", "2026-01-02T00:00:00+00:00",
            "2026-01-03T00:00:00+00:00", "2026-01-03T01:00:00+00:00",
            "2026-01-04T00:00:00+00:00"])
        self.assertEqual(stats["distinct_strategy_versions"], 2)
        self.assertEqual(stats["current_strategy_version"], B)
        self.assertEqual(stats["current_strategy_version_since"], "2026-01-02")
        self.assertEqual(stats["current_strategy_version_first_entry"], 2)
        self.assertEqual(stats["current_strategy_version_nights"], 2)

    def test_no_strategy_version_anywhere(self):
        stats = self._chain([{"type": "NIGHTLY_BRIEF"}], ["2026-01-01T00:00:00+00:00"])
        self.assertEqual(stats["distinct_strategy_versions"], 0)
        self.assertIsNone(stats["current_strategy_version"])


if __name__ == "__main__":
    unittest.main()
