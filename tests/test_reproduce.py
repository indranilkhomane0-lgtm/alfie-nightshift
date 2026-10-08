#!/usr/bin/env python3
"""
Tests for core/reproduce.py's Option B splice
(load_spliced_bars/apply_splice): recovering night N's live candle from
night N+1's own, provenance-verified archive.

No real N -> N+1 pair with actual archived bars exists yet as of
2026-10-08 (the only archived night so far, 2026-10-07, has no
successor night with any stamped predictions -- see the disclosure
entry covering that gap). These tests build a synthetic N+1 night
end to end -- a real OHLCV archive file, a real predictions.jsonl row
with a context_hash computed by the REAL nightshift.stamp_prediction
._context_hash(), gzip-compressed the same way
nightshift/archive_ohlcv.py writes it -- so the happy path is verified
against the actual verification logic, not a mocked stand-in for it.

stdlib + pandas/numpy (same as the real pipeline). Run directly:

    python3 tests/test_reproduce.py
"""
import gzip
import json
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "core"))

try:
    import pandas as pd
    import numpy as np
except ImportError:
    pd = None

import reproduce  # noqa: E402


def _make_df(dates):
    import pandas as pd
    idx = pd.to_datetime(dates)
    n = len(dates)
    return pd.DataFrame({
        "open": [100.0 + i for i in range(n)], "high": [101.0 + i for i in range(n)],
        "low": [99.0 + i for i in range(n)], "close": [100.5 + i for i in range(n)],
        "volume": [1_000_000.0 + i for i in range(n)],
    }, index=idx)


@unittest.skipIf(pd is None, "pandas/numpy not importable in this environment")
class SpliceTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.chain_path = self.root / "chain.jsonl"
        self.pred_path = self.root / "predictions.jsonl"
        self.archive_path = self.root / "ohlcv.csv.gz"
        self.chain_path.write_text("")
        self.pred_path.write_text("")
        self._patches = [
            mock.patch.object(reproduce, "CHAIN_PATH", self.chain_path),
            mock.patch.object(reproduce, "PREDICTIONS_PATH", self.pred_path),
            mock.patch.object(reproduce, "OHLCV_ARCHIVE_PATH", self.archive_path),
        ]
        for p in self._patches:
            p.start()
        self.logs = []

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self._tmp.cleanup()

    def log(self, line):
        self.logs.append(line)

    def _write_archive_night(self, night: date, asset: str, dates: list):
        """Appends `asset`'s closed-bar window for `night` as a real gzip
        member, in the exact CSV shape nightshift/archive_ohlcv.py writes,
        and a matching predictions.jsonl row whose context_hash is
        computed by the REAL _context_hash()."""
        from nightshift.stamp_prediction import _context_hash
        df = _make_df(dates)
        lines = []
        for idx, row in df.iterrows():
            lines.append(",".join([
                night.isoformat(), asset, idx.isoformat(), "binance",
                repr(float(row["open"])), repr(float(row["high"])),
                repr(float(row["low"])), repr(float(row["close"])),
                repr(float(row["volume"])),
            ]))
        with gzip.open(self.archive_path, "ab") as f:
            f.write(("\n".join(lines) + "\n").encode())

        h = _context_hash(df, "binance", None)
        pred = {
            "asset": asset, "config_id": f"{asset}_momentum_test",
            "cycle_date": night.isoformat(), "context_hash": h,
            "context_source": "binance", "context_fetched_at": None,
            "context_n_bars": len(df), "direction": "long",
        }
        with self.pred_path.open("a") as f:
            f.write(json.dumps(pred) + "\n")

    def test_target_is_today_refuses_same_day(self):
        with self.assertRaises(reproduce.NotReproducible) as ctx:
            reproduce.load_spliced_bars(date.today(), self.log)
        self.assertIn("SAME-DAY SPLICE OUT OF SCOPE", str(ctx.exception))

    def test_target_is_future_refuses_same_day(self):
        future = date.today() + timedelta(days=5)
        with self.assertRaises(reproduce.NotReproducible) as ctx:
            reproduce.load_spliced_bars(future, self.log)
        self.assertIn("SAME-DAY SPLICE OUT OF SCOPE", str(ctx.exception))

    def test_missing_n_plus_1_archive_entirely(self):
        target = date.today() - timedelta(days=2)
        # no archive file at all
        with self.assertRaises(reproduce.NotReproducible) as ctx:
            reproduce.load_spliced_bars(target, self.log)
        self.assertIn("NO N+1 ARCHIVE", str(ctx.exception))

    def test_n_plus_1_has_zero_predictions(self):
        """The exact real situation hit on 2026-10-08: night N+1's
        archive file may exist (other nights' data in it) but has
        nothing for N+1 itself because every candidate had no entry
        signal that night -- archive_closed_bars() never ran."""
        target = date.today() - timedelta(days=2)
        next_date = target + timedelta(days=1)
        # Archive has SOME data, just not for next_date / this asset.
        self._write_archive_night(next_date - timedelta(days=10), "BTC/USDT",
                                   [(next_date - timedelta(days=10 + i)).isoformat()
                                    for i in range(5)])
        with self.assertRaises(reproduce.NotReproducible) as ctx:
            reproduce.load_spliced_bars(target, self.log)
        self.assertIn("NO N+1 ARCHIVE", str(ctx.exception))
        self.assertIn("zero predictions", str(ctx.exception))

    def test_n_plus_1_context_hash_does_not_verify(self):
        """night N+1's archive exists and has a prediction row, but the
        recorded context_hash doesn't match what's actually archived
        (corruption, tampering, or a mismatched sidecar) -- must refuse
        to borrow from it regardless of what it contains."""
        target = date.today() - timedelta(days=2)
        next_date = target + timedelta(days=1)
        dates = [(target - timedelta(days=i)).isoformat() for i in range(5, 0, -1)] + [next_date.isoformat()]
        self._write_archive_night(next_date, "BTC/USDT", dates)
        # Corrupt the recorded hash in predictions.jsonl after the fact.
        rows = [json.loads(l) for l in self.pred_path.read_text().splitlines()]
        rows[-1]["context_hash"] = "0" * 64
        self.pred_path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")

        with self.assertRaises(reproduce.NotReproducible) as ctx:
            reproduce.load_spliced_bars(target, self.log)
        self.assertIn("NO N+1 ARCHIVE", str(ctx.exception))
        self.assertIn("does not verify", str(ctx.exception))

    def test_happy_path_splices_the_correct_bar(self):
        target = date.today() - timedelta(days=2)
        next_date = target + timedelta(days=1)
        # Night N+1's closed window: 5 days up through and including
        # `target` itself (target's bar is now closed, one day later).
        dates = [(target - timedelta(days=i)).isoformat() for i in range(4, 0, -1)] + [target.isoformat()]
        self._write_archive_night(next_date, "BTC/USDT", dates)

        spliced, verified = reproduce.load_spliced_bars(target, self.log)
        self.assertIn("BTC/USDT", verified)
        self.assertIn("BTC/USDT", spliced)
        row = spliced["BTC/USDT"]
        self.assertEqual(row.name.isoformat()[:10], target.isoformat())
        self.assertEqual(float(row["open"]), 100.0 + 4)  # last row of the 5-bar fixture

    def test_apply_splice_extends_the_dataframe_by_exactly_one_row(self):
        target = date.today() - timedelta(days=2)
        next_date = target + timedelta(days=1)
        dates = [(target - timedelta(days=i)).isoformat() for i in range(4, 0, -1)] + [target.isoformat()]
        self._write_archive_night(next_date, "BTC/USDT", dates)

        original = {"BTC/USDT": {"df": _make_df(
            [(target - timedelta(days=i)).isoformat() for i in range(4, 0, -1)]
        ), "source": "binance"}}
        self.assertEqual(len(original["BTC/USDT"]["df"]), 4)

        spliced = reproduce.apply_splice(original, target, self.log)
        self.assertEqual(len(spliced["BTC/USDT"]["df"]), 5)
        self.assertEqual(spliced["BTC/USDT"]["df"].index[-1].isoformat()[:10], target.isoformat())

    def test_asset_missing_from_n_plus_1_is_passed_through_unspliced(self):
        """One asset's bar missing from an otherwise-verified N+1 archive
        must not abort the whole run -- that asset is left at its
        original length, logged, not a hard failure, as long as at
        least one other asset DID splice successfully."""
        target = date.today() - timedelta(days=2)
        next_date = target + timedelta(days=1)
        dates_with_target = [(target - timedelta(days=i)).isoformat() for i in range(4, 0, -1)] + [target.isoformat()]
        self._write_archive_night(next_date, "BTC/USDT", dates_with_target)
        # ETH/USDT's N+1 window does NOT include target's date at all.
        dates_without_target = [(target - timedelta(days=i)).isoformat() for i in range(10, 5, -1)]
        self._write_archive_night(next_date, "ETH/USDT", dates_without_target)

        original = {
            "BTC/USDT": {"df": _make_df(dates_with_target[:-1]), "source": "binance"},
            "ETH/USDT": {"df": _make_df(dates_without_target), "source": "binance"},
        }
        spliced = reproduce.apply_splice(original, target, self.log)
        self.assertEqual(len(spliced["BTC/USDT"]["df"]), len(original["BTC/USDT"]["df"]) + 1)
        self.assertEqual(len(spliced["ETH/USDT"]["df"]), len(original["ETH/USDT"]["df"]))  # unchanged


if __name__ == "__main__":
    unittest.main()
