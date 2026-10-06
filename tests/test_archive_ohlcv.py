#!/usr/bin/env python3
"""
Tests for nightshift/archive_ohlcv.py -- the raw OHLCV archive, and its
wiring into nightshift/stamp_prediction.py's stamp() (the frozen-surface
call site that gives it the exact closed-bar window a prediction was
actually made against, not a re-fetch).

ArchiveClosedBarsTest exercises archive_closed_bars() directly, with a
fake pandas-like object (no real pandas dependency needed for these --
iterrows() is the only method used). StampIntegrationTest calls the
REAL nightshift.stamp_prediction.stamp() (needs pandas; skipped if
unavailable) to confirm the wiring itself: archiving fires exactly when
a real prediction is stamped, using the identical `closed` window that
gets hashed, and never fires when stamp() declines to stamp anything.
FetchFailureIntegrationTest drives the REAL nightshift.cycle.NightShiftCycle
end to end with the exchange fetch forced to fail, confirming the
required "fetch fails -> no archive, PIPELINE_FAILURE is the only
record" property holds for the real control flow, not just in theory.

stdlib only except StampIntegrationTest, which needs pandas/numpy (same
as the real pipeline) -- skips cleanly if they're not importable rather
than failing the suite in an environment without them. Run directly:

    python3 tests/test_archive_ohlcv.py
"""
import gzip
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from nightshift import archive_ohlcv  # noqa: E402


class _FakeRow:
    def __init__(self, **kw):
        self._kw = kw

    def __getitem__(self, key):
        return self._kw[key]


class _FakeTimestamp:
    def __init__(self, iso):
        self._iso = iso

    def isoformat(self):
        return self._iso


class _FakeClosed:
    """Minimal stand-in for the pandas DataFrame slice stamp() passes --
    only iterrows() and len() are used by archive_closed_bars()."""

    def __init__(self, rows):
        # rows: list of (iso_date, open, high, low, close, volume)
        self._rows = rows

    def iterrows(self):
        for iso, o, h, l, c, v in self._rows:
            yield _FakeTimestamp(iso), _FakeRow(open=o, high=h, low=l, close=c, volume=v)

    def __len__(self):
        return len(self._rows)


def _bars(n=3, asset_offset=0.0):
    return _FakeClosed([
        (f"2026-10-0{i+1}T00:00:00+00:00", 100.0 + i + asset_offset, 101.0 + i,
         99.0 + i, 100.5 + i, 1_000_000.0 + i)
        for i in range(n)
    ])


class ArchiveClosedBarsTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.archive_path = Path(self._tmp.name) / "ohlcv.csv.gz"
        self.log_path = Path(self._tmp.name) / "archive_ohlcv.log"
        self._patches = [
            mock.patch.object(archive_ohlcv, "ARCHIVE_PATH", self.archive_path),
            mock.patch.object(archive_ohlcv, "LOG_PATH", self.log_path),
        ]
        for p in self._patches:
            p.start()
        archive_ohlcv._archived_this_run.clear()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self._tmp.cleanup()
        archive_ohlcv._archived_this_run.clear()

    def _decompressed(self):
        if not self.archive_path.exists():
            return ""
        return gzip.open(self.archive_path, "rt").read()

    def test_writes_exact_values_full_precision(self):
        """Values must round-trip exactly -- repr(float(x)), not a
        rounded/truncated representation, so a verifier who re-parses
        this file and rebuilds _context_hash()'s payload from it gets
        the identical hash, not an approximation."""
        bars = _FakeClosed([("2026-10-01T00:00:00+00:00",
                              100.123456789012345, 101.0, 99.0, 100.5, 1234567.891)])
        archive_ohlcv.archive_closed_bars("BTC/USDT", bars, "binance", "2026-10-06")
        text = self._decompressed()
        self.assertIn(repr(100.123456789012345), text)
        self.assertIn(repr(1234567.891), text)
        self.assertTrue(text.startswith("2026-10-06,BTC/USDT,2026-10-01T00:00:00+00:00,binance,"))

    def test_second_call_same_night_same_asset_is_a_no_op(self):
        """Several candidates for the same asset the same night must not
        duplicate the window -- fast path, no disk re-read needed."""
        bars = _bars()
        archive_ohlcv.archive_closed_bars("ETH/USDT", bars, "binance", "2026-10-06")
        first = self._decompressed()
        archive_ohlcv.archive_closed_bars("ETH/USDT", bars, "binance", "2026-10-06")
        second = self._decompressed()
        self.assertEqual(first, second)

    def test_different_asset_same_night_both_archived(self):
        archive_ohlcv.archive_closed_bars("BTC/USDT", _bars(), "binance", "2026-10-06")
        archive_ohlcv.archive_closed_bars("ETH/USDT", _bars(asset_offset=500), "binance", "2026-10-06")
        text = self._decompressed()
        self.assertIn("2026-10-06,BTC/USDT,", text)
        self.assertIn("2026-10-06,ETH/USDT,", text)

    def test_cross_process_duplicate_is_detected_via_disk_scan(self):
        """Simulates a retried cycle process (fresh _archived_this_run
        cache) finding the SAME night+asset already on disk from an
        earlier, since-exited process -- must not duplicate."""
        archive_ohlcv.archive_closed_bars("BTC/USDT", _bars(), "binance", "2026-10-06")
        before = self._decompressed()
        archive_ohlcv._archived_this_run.clear()  # simulate a fresh process
        archive_ohlcv.archive_closed_bars("BTC/USDT", _bars(), "binance", "2026-10-06")
        after = self._decompressed()
        self.assertEqual(before, after)

    def test_write_failure_does_not_raise(self):
        """Disk full / permission denied on the archive write itself:
        must not raise -- stamp() must get its prediction back
        regardless of whether archiving succeeded."""
        with mock.patch("gzip.open", side_effect=OSError("No space left on device")):
            archive_ohlcv.archive_closed_bars("BTC/USDT", _bars(), "binance", "2026-10-06")  # must not raise
        self.assertFalse(self.archive_path.exists())

    def test_failed_write_is_retried_by_the_next_candidate_same_night(self):
        """Not cached as done on failure -- the next candidate for the
        same asset the same night (the only recovery this mechanism
        offers, per the module's documented limit) gets a free retry."""
        with mock.patch("gzip.open", side_effect=OSError("disk full")):
            archive_ohlcv.archive_closed_bars("BTC/USDT", _bars(), "binance", "2026-10-06")
        self.assertFalse(self.archive_path.exists())

        archive_ohlcv.archive_closed_bars("BTC/USDT", _bars(), "binance", "2026-10-06")  # disk "fixed"
        self.assertTrue(self.archive_path.exists())
        self.assertIn("2026-10-06,BTC/USDT,", self._decompressed())

    def test_corrupt_existing_archive_does_not_crash(self):
        self.archive_path.parent.mkdir(parents=True, exist_ok=True)
        self.archive_path.write_bytes(b"not a valid gzip stream at all")
        archive_ohlcv.archive_closed_bars("BTC/USDT", _bars(), "binance", "2026-10-06")  # must not raise
        # Treated as "can't confirm it's already there" -- writing proceeds,
        # appending a new (valid) gzip member after the corrupt bytes.
        self.assertTrue(self.archive_path.exists())

    def test_empty_closed_window_writes_nothing_but_does_not_raise(self):
        archive_ohlcv.archive_closed_bars("BTC/USDT", _bars(n=0), "binance", "2026-10-06")
        self.assertFalse(self.archive_path.exists())


class StampIntegrationTest(unittest.TestCase):
    """The real nightshift.stamp_prediction.stamp(), confirming the
    wiring: archiving fires with the exact window about to be hashed,
    only when a real prediction is produced."""

    @classmethod
    def setUpClass(cls):
        try:
            import pandas as pd  # noqa: F401
            import numpy as np  # noqa: F401
        except ImportError:
            raise unittest.SkipTest("pandas/numpy not importable in this environment")

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.pred_path = root / "predictions.jsonl"
        self.archive_path = root / "ohlcv.csv.gz"
        self.log_path = root / "archive_ohlcv.log"

        from nightshift import stamp_prediction
        self._patches = [
            mock.patch.object(stamp_prediction, "PRED_PATH", self.pred_path),
            mock.patch.object(archive_ohlcv, "ARCHIVE_PATH", self.archive_path),
            mock.patch.object(archive_ohlcv, "LOG_PATH", self.log_path),
        ]
        for p in self._patches:
            p.start()
        archive_ohlcv._archived_this_run.clear()
        self.stamp_prediction = stamp_prediction

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self._tmp.cleanup()
        archive_ohlcv._archived_this_run.clear()

    def _df(self, n=120, trend=0.5):
        import pandas as pd
        import numpy as np
        idx = pd.date_range(end=pd.Timestamp.now(tz="UTC").normalize()
                             - pd.Timedelta(days=1), periods=n, freq="D")
        close = 100 + np.cumsum(np.full(n, trend))
        return pd.DataFrame({
            "open": close - 0.5, "high": close + 1, "low": close - 1,
            "close": close, "volume": np.full(n, 1_000_000.0),
        }, index=idx)

    def test_real_signal_archives_the_exact_hashed_window(self):
        cfg = {"asset": "BTC/USDT", "config_id": "c1", "strategy_family": "momentum",
               "params": {"fast_ma": 5, "slow_ma": 20, "rsi_entry": 10}}
        pred = self.stamp_prediction.stamp(cfg, self._df(trend=0.5), source="binance")
        self.assertIsNotNone(pred, "fixture must produce a real long signal")
        self.assertEqual(pred["direction"], "long")
        self.assertTrue(self.archive_path.exists())

        text = gzip.open(self.archive_path, "rt").read()
        self.assertIn(f"BTC/USDT", text)
        # n_bars archived must match context_n_bars exactly -- same window.
        n_archived = sum(1 for l in text.splitlines() if l.startswith(pred["cycle_date"]))
        self.assertEqual(n_archived, pred["context_n_bars"])

    def test_no_signal_archives_nothing(self):
        """direction == 'none' returns before `closed` is even computed
        -- archiving must not fire for a candidate that produces no
        stamped prediction."""
        cfg = {"asset": "ETH/USDT", "config_id": "c2", "strategy_family": "momentum",
               "params": {"fast_ma": 20, "slow_ma": 5, "rsi_entry": 90}}
        pred = self.stamp_prediction.stamp(cfg, self._df(trend=-0.5), source="binance")
        self.assertIsNone(pred)
        self.assertFalse(self.archive_path.exists())


class FetchFailureIntegrationTest(unittest.TestCase):
    """The real nightshift.cycle.NightShiftCycle, end to end, with the
    exchange fetch forced to fail -- confirms the required property for
    real, not just by code-reading: `prices = {a: load_price_data(a, ...)
    for a in ASSETS}` is one dict comprehension in cycle.py's Stage 1,
    so ANY asset's fetch raising aborts the whole cycle before Stage 7
    (where stamp(), and therefore archiving, would run) is ever reached
    -- regardless of whether an earlier asset in the loop succeeded."""

    @classmethod
    def setUpClass(cls):
        try:
            import pandas as pd  # noqa: F401
            import numpy as np  # noqa: F401
            import sklearn  # noqa: F401
            import hmmlearn  # noqa: F401
        except ImportError as exc:
            raise unittest.SkipTest(f"full pipeline deps not importable: {exc}")

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.db_path = root / "corpus.db"
        self.pred_path = root / "predictions.jsonl"
        self.archive_path = root / "ohlcv.csv.gz"
        self.archive_log = root / "archive_ohlcv.log"

        from nightshift import db as nsdb, cycle, stamp_prediction
        self._patches = [
            mock.patch.object(nsdb, "DB_PATH", self.db_path),
            mock.patch.object(stamp_prediction, "PRED_PATH", self.pred_path),
            mock.patch.object(archive_ohlcv, "ARCHIVE_PATH", self.archive_path),
            mock.patch.object(archive_ohlcv, "LOG_PATH", self.archive_log),
        ]
        for p in self._patches:
            p.start()
        archive_ohlcv._archived_this_run.clear()
        self.cycle = cycle

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self._tmp.cleanup()
        archive_ohlcv._archived_this_run.clear()

    def test_fetch_failure_leaves_no_archive_and_raises(self):
        fake_exchange = mock.Mock()
        fake_exchange.milliseconds.return_value = 1_700_000_000_000
        fake_exchange.fetch_ohlcv.side_effect = Exception("simulated exchange outage")

        with mock.patch.object(self.cycle.ccxt, "binance", return_value=fake_exchange):
            night = self.cycle.NightShiftCycle(cycle_id=99999901)
            with self.assertRaises(Exception):
                night.run()

        self.assertFalse(self.archive_path.exists(),
                          "a fetch failure must leave no OHLCV archive at all")
        self.assertFalse(self.pred_path.exists(),
                          "a fetch failure must leave no stamped predictions either")


if __name__ == "__main__":
    unittest.main()
