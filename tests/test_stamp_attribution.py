#!/usr/bin/env python3
"""
Tests for stamp-time strategy_version attribution: the real
nightshift.stamp_prediction.stamp() writing strategy_version onto each
row, and nightshift.label_outcomes.outcome_payload() carrying it onto
LABELED_OUTCOME as stamped_strategy_version.

Also pins the reproducibility contract the change must not touch:
_context_hash()'s inputs (the closed-bar window, source, fetched_at)
and every pre-existing row field are unchanged -- core/reproduce.py
recomputes context_hash from exactly those inputs and diffs exactly
those fields.

Run directly:

    python3 tests/test_stamp_attribution.py
"""
import inspect
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from nightshift import archive_ohlcv  # noqa: E402
from nightshift import label_outcomes  # noqa: E402
from nightshift.strategy_version import compute_strategy_version  # noqa: E402

# Every key a stamped row carried before this change. The new row must
# carry all of these, unchanged in meaning, plus strategy_version only.
PRE_EXISTING_ROW_KEYS = {
    "prediction_id", "cycle_date", "asset", "config_id", "strategy_family",
    "direction", "entry_signal_detail", "entry_price", "hold_days",
    "settle_date", "wfo_win_rate", "regime_state", "wfo_n_trials",
    "wfo_n_candidates_surviving_min_sharpe", "context_hash",
    "context_source", "context_start", "context_end", "context_n_bars",
    "context_fetched_at", "status",
}


class StampWritesStrategyVersionTest(unittest.TestCase):

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
        from nightshift import stamp_prediction
        self.sp = stamp_prediction
        self._patches = [
            mock.patch.object(stamp_prediction, "PRED_PATH", self.pred_path),
            mock.patch.object(stamp_prediction, "_stamp_strategy_version", None),
            mock.patch.object(archive_ohlcv, "ARCHIVE_PATH", root / "ohlcv.csv.gz"),
            mock.patch.object(archive_ohlcv, "LOG_PATH", root / "archive_ohlcv.log"),
        ]
        for p in self._patches:
            p.start()
        archive_ohlcv._archived_this_run.clear()

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

    def _cfg(self, cid="c1", asset="BTC/USDT"):
        return {"asset": asset, "config_id": cid, "strategy_family": "momentum",
                "params": {"fast_ma": 5, "slow_ma": 20, "rsi_entry": 10}}

    def test_row_carries_current_strategy_version(self):
        pred = self.sp.stamp(self._cfg(), self._df(), source="binance")
        self.assertEqual(pred["strategy_version"], compute_strategy_version())
        on_disk = json.loads(self.pred_path.read_text().splitlines()[0])
        self.assertEqual(on_disk["strategy_version"], pred["strategy_version"])

    def test_only_strategy_version_is_added_to_the_row(self):
        pred = self.sp.stamp(self._cfg(), self._df(), source="binance")
        self.assertEqual(set(pred), PRE_EXISTING_ROW_KEYS | {"strategy_version"})

    def test_context_hash_inputs_unchanged(self):
        """context_hash is still _context_hash(closed, source, fetched_at)
        over the closed-bar window -- the exact recomputation
        core/reproduce.py performs."""
        params = list(inspect.signature(self.sp._context_hash).parameters)
        self.assertEqual(params, ["ohlcv", "source", "fetched_at"])
        df = self._df()
        pred = self.sp.stamp(self._cfg(), df, source="binance")
        from core.bar_calendar import is_utc_daily_bar_complete
        closed = df[[is_utc_daily_bar_complete(i) for i in df.index]]
        self.assertEqual(pred["context_hash"],
                         self.sp._context_hash(closed, "binance", None))

    def test_computed_once_per_process(self):
        with mock.patch.object(self.sp, "compute_strategy_version",
                               return_value="x" * 64) as m:
            a = self.sp.stamp(self._cfg("c1"), self._df(), source="binance")
            b = self.sp.stamp(self._cfg("c2", "ETH/USDT"), self._df(), source="binance")
        self.assertEqual(m.call_count, 1)
        self.assertEqual(a["strategy_version"], b["strategy_version"])

    def test_hash_failure_records_unknown_and_still_stamps(self):
        with mock.patch.object(self.sp, "compute_strategy_version",
                               side_effect=OSError("missing file")):
            pred = self.sp.stamp(self._cfg(), self._df(), source="binance")
        self.assertIsNotNone(pred)
        self.assertEqual(pred["strategy_version"], "unknown")


class OutcomePayloadTest(unittest.TestCase):

    def _row(self, **extra):
        r = {"prediction_id": "p1", "asset": "BTC/USDT", "config_id": "c1",
             "direction": "long", "entry_price": 100.0, "settle_close": 101.0,
             "return_pct": 1.0, "outcome": "WIN", "cycle_date": "2026-10-27",
             "settle_date": "2026-11-03"}
        r.update(extra)
        return r

    def test_stamped_version_carried_under_its_own_key(self):
        payload = label_outcomes.outcome_payload(self._row(strategy_version="s" * 64))
        self.assertEqual(payload["stamped_strategy_version"], "s" * 64)
        # Never as "strategy_version": append_entry() overwrites that key
        # with the grading-time value.
        self.assertNotIn("strategy_version", payload)

    def test_row_without_field_gets_no_key(self):
        payload = label_outcomes.outcome_payload(self._row())
        self.assertNotIn("stamped_strategy_version", payload)

    def test_append_entry_keeps_stamped_key_beside_grading_time_key(self):
        from nightshift import publish_chain
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        chain = Path(tmp.name) / "chain.jsonl"
        # _stamp_at_publish_time writes reports/ots/ and calls OTS
        # calendars -- never from a test.
        with mock.patch.object(publish_chain, "CHAIN_PATH", chain), \
             mock.patch.object(publish_chain, "_stamp_at_publish_time"), \
             mock.patch.object(publish_chain, "_strategy_version", return_value="g" * 64):
            entry = publish_chain.append_entry(
                label_outcomes.outcome_payload(self._row(strategy_version="s" * 64)))
        self.assertEqual(entry["payload"]["stamped_strategy_version"], "s" * 64)
        self.assertEqual(entry["payload"]["strategy_version"], "g" * 64)


if __name__ == "__main__":
    unittest.main()
