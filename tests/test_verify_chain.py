#!/usr/bin/env python3
"""
Tests for core/verify_chain.py's return/date statistics -- the numbers
added so the verifier reports payoff magnitude and graduation progress,
not just win/loss counts. Builds small synthetic chains (properly
hash-linked, exactly as publish_chain.append_entry does) in a temp file
per test rather than touching reports/chain.jsonl.

stdlib only, matching core/verify_chain.py's own zero-dependency stance.
Run directly:

    python3 tests/test_verify_chain.py
"""
import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "core"))
from verify_chain import compute_stats, FROZEN_CODE_VERSION  # noqa: E402

GENESIS_HASH = "0" * 64
OTHER_CODE_VERSION = "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef"


def _canonical(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":")).encode()


def _write_chain(payloads) -> Path:
    """Hash-link payloads exactly as publish_chain.append_entry does and
    write them to a fresh temp file; returns the path."""
    tmp = tempfile.NamedTemporaryFile(
        mode="w", suffix=".jsonl", delete=False, dir=tempfile.gettempdir()
    )
    prev = GENESIS_HASH
    with tmp:
        for payload in payloads:
            entry_hash = hashlib.sha256(prev.encode() + _canonical(payload)).hexdigest()
            entry = {
                "published_at_utc": "2026-01-01T00:00:00+00:00",
                "prev_hash": prev,
                "payload": payload,
                "entry_hash": entry_hash,
            }
            tmp.write(json.dumps(entry, sort_keys=True) + "\n")
            prev = entry_hash
    return Path(tmp.name)


def _outcome(asset, direction, cycle_date, settle_date, outcome, return_pct,
             code_version=OTHER_CODE_VERSION):
    return {
        "type": "LABELED_OUTCOME",
        "asset": asset, "direction": direction, "cycle_date": cycle_date,
        "settle_date": settle_date, "outcome": outcome,
        "return_pct": return_pct, "code_version": code_version,
    }


class ReturnStatsTest(unittest.TestCase):
    """Task 2, item 1: average and median return per graded call,
    separately for wins and losses."""

    def test_avg_and_median_split_by_outcome(self):
        payloads = [
            _outcome("BTC/USDT", "long", "2026-01-01", "2026-01-08", "WIN", 2.0),
            _outcome("ETH/USDT", "long", "2026-01-01", "2026-01-08", "WIN", 4.0),
            _outcome("SOL/USDT", "long", "2026-01-01", "2026-01-08", "LOSS", -1.0),
            _outcome("BTC/USDT", "long", "2026-01-02", "2026-01-09", "LOSS", -3.0),
        ]
        path = _write_chain(payloads)
        stats = compute_stats(path)
        win_avg, win_median, win_n = stats["return_stats"]["win"]
        loss_avg, loss_median, loss_n = stats["return_stats"]["loss"]
        self.assertEqual(win_n, 2)
        self.assertAlmostEqual(win_avg, 3.0)
        self.assertAlmostEqual(win_median, 3.0)
        self.assertEqual(loss_n, 2)
        self.assertAlmostEqual(loss_avg, -2.0)
        self.assertAlmostEqual(loss_median, -2.0)

    def test_empty_side_reports_none_not_zero(self):
        """No losses on record must show up as absent, not as a fabricated
        0.0% average -- 0.0 would misreport an absence of data as a call
        that broke exactly even."""
        payloads = [
            _outcome("BTC/USDT", "long", "2026-01-01", "2026-01-08", "WIN", 1.0),
        ]
        stats = compute_stats(_write_chain(payloads))
        loss_avg, loss_median, loss_n = stats["return_stats"]["loss"]
        self.assertEqual(loss_n, 0)
        self.assertIsNone(loss_avg)
        self.assertIsNone(loss_median)


class LabeledDatesTest(unittest.TestCase):
    """Task 2, item 2: distinct labeled DATES, not calls -- what
    META_MIN_SAMPLES actually counts."""

    def test_counts_distinct_settle_dates_not_calls(self):
        payloads = [
            # Two distinct calls settling the same night -> one date.
            _outcome("BTC/USDT", "long", "2026-01-01", "2026-01-08", "WIN", 1.0),
            _outcome("ETH/USDT", "long", "2026-01-01", "2026-01-08", "LOSS", -1.0),
            # A third call settling a different night -> a second date.
            _outcome("SOL/USDT", "long", "2026-01-02", "2026-01-09", "WIN", 2.0),
            # A NO_CALL row must not count -- the system made no call.
            _outcome("BTC/USDT", "long", "2026-01-03", "2026-01-10", "NO_CALL", 0.0),
        ]
        stats = compute_stats(_write_chain(payloads))
        self.assertEqual(stats["labeled_dates_count"], 2)


class FrozenVersionReturnStatsTest(unittest.TestCase):
    """Task 2, item 3: the same return numbers again, restricted to the
    frozen code_version -- the only record that will be sellable."""

    def test_filters_out_other_code_versions(self):
        payloads = [
            _outcome("BTC/USDT", "long", "2026-01-01", "2026-01-08", "WIN", 5.0,
                     code_version=FROZEN_CODE_VERSION),
            _outcome("ETH/USDT", "long", "2026-01-01", "2026-01-08", "LOSS", -2.0,
                     code_version=FROZEN_CODE_VERSION),
            # Pre-freeze calls under other code versions must not leak in,
            # however large -- these would blow out the averages if they did.
            _outcome("SOL/USDT", "long", "2026-01-02", "2026-01-09", "WIN", 100.0,
                     code_version=OTHER_CODE_VERSION),
            _outcome("BTC/USDT", "long", "2026-01-03", "2026-01-10", "LOSS", -100.0,
                     code_version=OTHER_CODE_VERSION),
        ]
        stats = compute_stats(_write_chain(payloads))

        # Unfiltered numbers see all four calls.
        self.assertEqual(stats["return_stats"]["win"][2], 2)
        self.assertEqual(stats["return_stats"]["loss"][2], 2)

        # Frozen-version numbers see only the two chained under the freeze.
        win_avg, win_median, win_n = stats["frozen_return_stats"]["win"]
        loss_avg, loss_median, loss_n = stats["frozen_return_stats"]["loss"]
        self.assertEqual(win_n, 1)
        self.assertAlmostEqual(win_avg, 5.0)
        self.assertEqual(loss_n, 1)
        self.assertAlmostEqual(loss_avg, -2.0)


if __name__ == "__main__":
    unittest.main()
