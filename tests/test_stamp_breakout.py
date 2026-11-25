#!/usr/bin/env python3
"""
Tests for the entry-478 breakout fix in nightshift/stamp_prediction.py.

Three groups:

  1. Pure entry_signal() breakout tests: fires, does not fire, the
     fail-closed default when closed_closes is omitted, the strict `>`
     boundary, the lb/xlb >= len-5 guard, and parity with the real
     backtester (strategies/__init__.py breakout_strategy) at the last bar.

  2. stamp() and the forming bar: a DataFrame whose last row is dated
     today UTC (the in-progress bar) with a close above the channel must
     produce no stamp; the same breakout on the last CLOSED bar must
     stamp. Bars are judged closed by core/bar_calendar, never by
     position (no closes[:-1]).

  3. Regression, generation 12 vs 13: mean_rev, momentum and relative
     produce IDENTICAL entry_signal() output and IDENTICAL stamp() rows
     under the pre-change stamp_prediction.py (loaded from git at
     GEN12_COMMIT) and the changed one, on the same fixtures and params.
     This is what makes generation 13 a breakout-only change rather than
     a stamp() change that touched four families.

Run via the isolation guard:

    python3 tests/run_isolated.py
"""
import importlib.util
import random
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from nightshift import archive_ohlcv  # noqa: E402
from nightshift import stamp_prediction  # noqa: E402
from nightshift.stamp_prediction import entry_signal  # noqa: E402

# Last commit before the breakout fix: generation 12's stamp_prediction.py.
GEN12_COMMIT = "a49b56050c7e11dd0aa9e7e5b72c577b0965070b"

BO = {"lookback": 20, "exit_lookback": 10, "max_hold_days": 10}


def _flat_then(last, n=60, level=100.0):
    """n-1 closed closes at `level` with one bump to 105 well inside the
    channel window, then `last`. Prior 20-bar max is exactly 105."""
    s = [level] * (n - 1)
    s[-5] = 105.0
    return s + [last]


class BreakoutEntrySignalTest(unittest.TestCase):

    def test_fires_when_close_exceeds_prior_lookback_high(self):
        d, diag = entry_signal("breakout", BO, closes=[], closed_closes=_flat_then(110.0))
        self.assertEqual(d, "long")
        self.assertEqual(diag["upper"], 105.0)
        self.assertEqual(diag["close"], 110.0)

    def test_does_not_fire_below_prior_lookback_high(self):
        d, diag = entry_signal("breakout", BO, closes=[], closed_closes=_flat_then(101.0))
        self.assertEqual(d, "none")
        self.assertEqual(diag["upper"], 105.0)

    def test_channel_excludes_the_bar_itself(self):
        """shift(1): the tested bar is not in its own channel. If it were,
        a new high could never exceed the max and this would never fire."""
        s = [100.0 + i for i in range(60)]   # every bar a new high
        d, diag = entry_signal("breakout", BO, closes=[], closed_closes=s)
        self.assertEqual(d, "long")
        self.assertEqual(diag["upper"], s[-2])

    def test_fails_closed_without_closed_closes(self):
        s = _flat_then(110.0)   # would fire if read
        d, diag = entry_signal("breakout", BO, s)
        self.assertEqual(d, "none")
        self.assertIn("requires closed bars", diag["note"])

    def test_strict_greater_than_boundary(self):
        self.assertEqual(entry_signal("breakout", BO, [], closed_closes=_flat_then(105.0))[0], "none")
        self.assertEqual(entry_signal("breakout", BO, [], closed_closes=_flat_then(105.0 + 1e-9))[0], "long")

    def test_lookback_guard(self):
        # lb >= n-5 -> no signal, even on a clear breakout.
        s = [100.0] * 24 + [200.0]             # n = 25, n-5 = 20
        d, diag = entry_signal("breakout", {"lookback": 20, "exit_lookback": 5}, [], closed_closes=s)
        self.assertEqual(d, "none")
        self.assertIn("guard", diag["note"])
        # one more bar: lb = 20 < n-5 = 21 -> evaluated, fires.
        s2 = [100.0] * 25 + [200.0]
        self.assertEqual(entry_signal("breakout", {"lookback": 20, "exit_lookback": 5}, [], closed_closes=s2)[0], "long")
        # xlb is guarded too.
        d3, diag3 = entry_signal("breakout", {"lookback": 10, "exit_lookback": 21}, [], closed_closes=s2)
        self.assertEqual(d3, "none")
        self.assertIn("guard", diag3["note"])

    def test_nan_in_either_window_does_not_fire(self):
        s = _flat_then(110.0)
        s_up = list(s); s_up[-15] = float("nan")   # inside lb=20 window only
        s_lo = list(s); s_lo[-3] = float("nan")    # inside xlb=10 window
        self.assertEqual(entry_signal("breakout", BO, [], closed_closes=s_up)[0], "none")
        self.assertEqual(entry_signal("breakout", BO, [], closed_closes=s_lo)[0], "none")

    def test_parity_with_backtester_at_last_bar(self):
        """Against the REAL breakout_strategy(): capture its signal series
        (patching _safe_ret to return it). Wherever the backtester is flat
        going into the last bar (sig[-2] == 0), it enters on the last bar
        exactly when entry_signal() says long."""
        try:
            import numpy as np
            import pandas as pd
            from nightshift import strategies
        except ImportError:
            self.skipTest("pandas/numpy not importable")
        rng = random.Random(478)
        seen = {"long": 0, "none": 0}
        checked = 0
        with mock.patch.object(strategies, "_safe_ret", lambda prices, sig: sig):
            for _ in range(400):
                n = rng.randint(40, 160)
                lb = rng.randint(10, 60)
                xlb = rng.randint(5, max(6, lb // 2))
                params = {"lookback": lb, "exit_lookback": xlb, "max_hold_days": rng.randint(3, 20)}
                c = [100.0]
                for _ in range(n - 1):
                    c.append(c[-1] * (1 + rng.gauss(0.002, 0.03)))
                prices = pd.DataFrame({"close": c}, index=pd.date_range("2026-01-01", periods=n, freq="D"))
                sig = strategies.breakout_strategy(prices, params)
                if isinstance(sig, np.ndarray):        # guard path returns zeros
                    sig_last, sig_prev = float(sig[-1]), float(sig[-2])
                else:
                    sig_last, sig_prev = float(sig.iloc[-1]), float(sig.iloc[-2])
                if sig_prev != 0.0:
                    continue
                d, _ = entry_signal("breakout", params, [], closed_closes=c)
                self.assertEqual(d == "long", sig_last == 1.0, (n, params))
                seen[d] += 1
                checked += 1
        self.assertGreater(seen["long"], 0, "fixture never exercised an entry")
        self.assertGreater(seen["none"], 0, "fixture never exercised a non-entry")
        self.assertGreater(checked, 100)


class _StampHarness(unittest.TestCase):
    """Patches every path stamp() writes: predictions.jsonl and the OHLCV
    archive and its log."""

    @classmethod
    def setUpClass(cls):
        try:
            import pandas  # noqa: F401
            import numpy  # noqa: F401
        except ImportError:
            raise unittest.SkipTest("pandas/numpy not importable")

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.pred_path = root / "predictions.jsonl"
        self._patches = [
            mock.patch.object(stamp_prediction, "PRED_PATH", self.pred_path),
            mock.patch.object(archive_ohlcv, "ARCHIVE_PATH", root / "ohlcv.csv.gz"),
            mock.patch.object(archive_ohlcv, "LOG_PATH", root / "archive_ohlcv.log"),
        ]
        for p in self._patches:
            p.start()
        archive_ohlcv._archived_this_run.clear()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        archive_ohlcv._archived_this_run.clear()
        self._tmp.cleanup()

    @staticmethod
    def _df(closes, last_day_offset):
        """Daily bars, tz-naive UTC midnight like cycle.load_price_data().
        last_day_offset 0: last row is today (in progress); -1: yesterday."""
        import pandas as pd
        end = pd.Timestamp.now(tz="UTC").normalize().tz_localize(None) + pd.Timedelta(days=last_day_offset)
        idx = pd.date_range(end=end, periods=len(closes), freq="D")
        return pd.DataFrame({"open": closes, "high": [c + 1 for c in closes],
                             "low": [c - 1 for c in closes], "close": closes,
                             "volume": [1e6] * len(closes)}, index=idx)


class BreakoutFormingBarTest(_StampHarness):
    CFG = {"asset": "BTC/USDT", "config_id": "bo1", "strategy_family": "breakout",
           "params": dict(BO)}

    def test_forming_bar_above_channel_does_not_stamp(self):
        closes = _flat_then(101.0, n=120) + [150.0]     # 150 is today's in-progress bar
        df = self._df(closes, last_day_offset=0)
        pred = stamp_prediction.stamp(dict(self.CFG), df, source="binance")
        self.assertIsNone(pred)
        self.assertFalse(self.pred_path.exists())

    def test_breakout_on_last_closed_bar_stamps_with_forming_bar_present(self):
        closes = _flat_then(150.0, n=120) + [101.0]     # breakout yesterday, today in progress
        df = self._df(closes, last_day_offset=0)
        pred = stamp_prediction.stamp(dict(self.CFG), df, source="binance")
        self.assertIsNotNone(pred)
        self.assertEqual(pred["direction"], "long")
        self.assertEqual(pred["entry_signal_detail"]["close"], 150.0)
        # entry_price is unchanged behaviour: closes[-1], the forming bar.
        self.assertEqual(pred["entry_price"], 101.0)

    def test_no_forming_bar_last_element_is_used(self):
        """Fetch ending on a completed bar: the last element IS closed and
        must be tested -- the reason closes[:-1] is wrong."""
        closes = _flat_then(150.0, n=120)
        df = self._df(closes, last_day_offset=-1)
        pred = stamp_prediction.stamp(dict(self.CFG), df, source="binance")
        self.assertIsNotNone(pred)
        self.assertEqual(pred["entry_signal_detail"]["close"], 150.0)
        self.assertEqual(pred["entry_signal_detail"]["n_closed"], 120)


def _load_gen12_module(tmpdir: Path):
    src = subprocess.run(["git", "-C", str(ROOT), "show", f"{GEN12_COMMIT}:nightshift/stamp_prediction.py"],
                         capture_output=True, text=True, check=True).stdout
    p = tmpdir / "stamp_prediction_gen12.py"
    p.write_text(src)
    spec = importlib.util.spec_from_file_location("stamp_prediction_gen12", p)
    mod = importlib.util.module_from_spec(spec)
    saved = list(sys.path)
    try:
        spec.loader.exec_module(mod)
    finally:
        sys.path[:] = saved
    return mod


class SiblingFamiliesUnchangedTest(_StampHarness):
    """mean_rev, momentum, relative: identical before (generation 12, from
    git) and after (this tree), same fixtures, same params."""

    PARAMS = {
        "mean_rev": [{"rsi_period": 14, "rsi_low": 30, "rsi_high": 70},
                     {"rsi_period": 7, "rsi_low": 45, "rsi_high": 60},
                     {"rsi_period": 21, "rsi_low": 55, "rsi_high": 80}],
        "momentum": [{"fast_ma": 5, "slow_ma": 20, "rsi_entry": 10},
                     {"fast_ma": 18, "slow_ma": 72, "rsi_entry": 38},
                     {"fast_ma": 20, "slow_ma": 5, "rsi_entry": 90}],
        "relative": [{"lookback": 14, "entry_z": 1.5},
                     {"lookback": 7, "entry_z": 0.0},
                     {"lookback": 3, "entry_z": -0.5}],
    }

    def setUp(self):
        super().setUp()
        self.gen12 = _load_gen12_module(Path(self._tmp.name))
        p = mock.patch.object(self.gen12, "PRED_PATH", Path(self._tmp.name) / "gen12_predictions.jsonl")
        p.start()
        self._patches.append(p)
        # Proof the "before" side is the pre-fix code: it has no breakout branch.
        d, diag = self.gen12.entry_signal("breakout", dict(BO), _flat_then(110.0))
        self.assertEqual((d, diag), ("none", {"note": "unknown family breakout"}))

    def _series(self):
        rng = random.Random(12)
        out = [[100.0 + 0.5 * i for i in range(150)],      # steady rise
               [200.0 - 0.5 * i for i in range(150)],      # steady fall
               [100.0 + (i % 3) for i in range(150)]]      # flat
        for _ in range(9):                                 # random walks
            c = [100.0]
            for _ in range(149):
                c.append(c[-1] * (1 + rng.gauss(0, 0.03)))
            out.append(c)
        return out

    def test_entry_signal_identical(self):
        n = 0
        for fam, plist in self.PARAMS.items():
            for params in plist:
                for s in self._series():
                    before = self.gen12.entry_signal(fam, params, s)
                    self.assertEqual(entry_signal(fam, params, s), before, (fam, params))
                    self.assertEqual(entry_signal(fam, params, s, closed_closes=s[:-1]), before, (fam, params))
                    self.assertEqual(entry_signal(fam, params, s, closed_closes=[]), before, (fam, params))
                    n += 1
        self.assertEqual(n, 3 * 3 * 12)

    def test_stamp_rows_identical(self):
        seen = {f: {"long": 0, "none": 0} for f in self.PARAMS}
        for fam, plist in self.PARAMS.items():
            for i, params in enumerate(plist):
                for j, s in enumerate(self._series()):
                    for offset in (0, -1):      # with and without a forming bar
                        df = self._df(s, last_day_offset=offset)
                        cfg = {"asset": "ETH/USDT", "config_id": f"{fam}-{i}-{j}",
                               "strategy_family": fam, "params": params,
                               "wfo_win_rate": 0.6, "regime_state": 0}
                        archive_ohlcv._archived_this_run.clear()
                        before = self.gen12.stamp(dict(cfg), df.copy(), source="binance", fetched_at="t")
                        archive_ohlcv._archived_this_run.clear()
                        after = stamp_prediction.stamp(dict(cfg), df.copy(), source="binance", fetched_at="t")
                        self.assertEqual(after, before, (fam, params, j, offset))
                        seen[fam]["none" if after is None else "long"] += 1
        for fam, c in seen.items():
            self.assertGreater(c["long"], 0, f"{fam}: fixtures never produced a stamp")
            self.assertGreater(c["none"], 0, f"{fam}: fixtures never produced a non-stamp")


if __name__ == "__main__":
    unittest.main(verbosity=2)
