#!/usr/bin/env python3
"""
Tests for core/verify_chain.py's return/date statistics -- the numbers
added so the verifier reports payoff magnitude and graduation progress,
not just win/loss counts -- and for nightshift/strategy_version.py, the
content hash the freeze was repointed to. Builds small synthetic chains
(properly hash-linked, exactly as publish_chain.append_entry does) in a
temp file per test rather than touching reports/chain.jsonl, and small
fake file trees for the strategy_version tests rather than touching the
real nightshift/ source.

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
sys.path.insert(0, str(ROOT))
from verify_chain import (  # noqa: E402
    compute_stats, FROZEN_CODE_VERSION, FROZEN_STRATEGY_VERSION,
    ANCHORING_START, OTS_MAGIC,
)
from nightshift.strategy_version import (  # noqa: E402
    compute_strategy_version, STRATEGY_VERSION_FILES,
)

GENESIS_HASH = "0" * 64
OTHER_CODE_VERSION = "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef"
OTHER_STRATEGY_VERSION = "beefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdead"
# Before ANCHORING_START (2026-07-28) -- the existing tests all use this
# date, which incidentally means none of them are anchoring-eligible, so
# the receipt checks below stay silent for every payload constructed by
# _outcome()/_write_chain() unless a test explicitly asks for a later date.
PRE_ANCHORING_DATE = "2026-01-01T00:00:00+00:00"
POST_ANCHORING_DATE = "2026-09-16T00:00:00+00:00"


def _canonical(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":")).encode()


def _write_chain(payloads, published_at_utc=PRE_ANCHORING_DATE) -> Path:
    """Hash-link payloads exactly as publish_chain.append_entry does and
    write them to a fresh temp file; returns the path. published_at_utc
    is a single date applied to every payload unless a list matching
    len(payloads) is given, for tests that need per-entry dates (e.g.
    mixing pre- and post-anchoring entries in one chain)."""
    dates = (published_at_utc if isinstance(published_at_utc, list)
              else [published_at_utc] * len(payloads))
    assert len(dates) == len(payloads)
    tmp = tempfile.NamedTemporaryFile(
        mode="w", suffix=".jsonl", delete=False, dir=tempfile.gettempdir()
    )
    prev = GENESIS_HASH
    with tmp:
        for payload, pub in zip(payloads, dates):
            entry_hash = hashlib.sha256(prev.encode() + _canonical(payload)).hexdigest()
            entry = {
                "published_at_utc": pub,
                "prev_hash": prev,
                "payload": payload,
                "entry_hash": entry_hash,
            }
            tmp.write(json.dumps(entry, sort_keys=True) + "\n")
            prev = entry_hash
    return Path(tmp.name)


def _outcome(asset, direction, cycle_date, settle_date, outcome, return_pct,
             code_version=OTHER_CODE_VERSION, strategy_version="__unset__"):
    payload = {
        "type": "LABELED_OUTCOME",
        "asset": asset, "direction": direction, "cycle_date": cycle_date,
        "settle_date": settle_date, "outcome": outcome,
        "return_pct": return_pct, "code_version": code_version,
    }
    # "__unset__" (default) omits the key entirely -- simulates a real
    # historical entry chained before strategy_version existed. Pass
    # strategy_version=None explicitly only if you actually mean a stored
    # JSON null, which is a different thing from the key being absent.
    if strategy_version != "__unset__":
        payload["strategy_version"] = strategy_version
    return payload


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


class FrozenCodeVersionReturnStatsTest(unittest.TestCase):
    """Task 2, item 3 (original): the same return numbers again, restricted
    to the frozen code_version -- kept as real history after the repoint,
    see core/verify_chain.py's FROZEN_CODE_VERSION docstring."""

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
        win_avg, win_median, win_n = stats["frozen_code_version_return_stats"]["win"]
        loss_avg, loss_median, loss_n = stats["frozen_code_version_return_stats"]["loss"]
        self.assertEqual(win_n, 1)
        self.assertAlmostEqual(win_avg, 5.0)
        self.assertEqual(loss_n, 1)
        self.assertAlmostEqual(loss_avg, -2.0)


class FrozenStrategyVersionReturnStatsTest(unittest.TestCase):
    """The repointed freeze: same filtering, keyed on strategy_version
    instead of code_version. Includes the "absent field on historical
    entries doesn't crash or fabricate" case -- entries chained before
    this field existed must be silently excluded, not treated as a match,
    a non-match masquerading as 0, or a crash."""

    def test_filters_out_other_strategy_versions(self):
        payloads = [
            _outcome("BTC/USDT", "long", "2026-01-01", "2026-01-08", "WIN", 5.0,
                     strategy_version=FROZEN_STRATEGY_VERSION),
            _outcome("ETH/USDT", "long", "2026-01-01", "2026-01-08", "LOSS", -2.0,
                     strategy_version=FROZEN_STRATEGY_VERSION),
            # A different (e.g. pre-correction) strategy_version must not
            # leak in, however large.
            _outcome("SOL/USDT", "long", "2026-01-02", "2026-01-09", "WIN", 100.0,
                     strategy_version=OTHER_STRATEGY_VERSION),
            _outcome("BTC/USDT", "long", "2026-01-03", "2026-01-10", "LOSS", -100.0,
                     strategy_version=OTHER_STRATEGY_VERSION),
        ]
        stats = compute_stats(_write_chain(payloads))
        win_avg, win_median, win_n = stats["frozen_strategy_version_return_stats"]["win"]
        loss_avg, loss_median, loss_n = stats["frozen_strategy_version_return_stats"]["loss"]
        self.assertEqual(win_n, 1)
        self.assertAlmostEqual(win_avg, 5.0)
        self.assertEqual(loss_n, 1)
        self.assertAlmostEqual(loss_avg, -2.0)

    def test_entries_predating_the_field_are_excluded_not_crashed_not_fabricated(self):
        payloads = [
            # No strategy_version key at all -- exactly what every entry
            # chained before the repoint looks like on disk.
            _outcome("BTC/USDT", "long", "2026-01-01", "2026-01-08", "WIN", 5.0),
            _outcome("ETH/USDT", "long", "2026-01-01", "2026-01-08", "LOSS", -2.0),
        ]
        # Must not raise (KeyError, TypeError from comparing None, etc.).
        stats = compute_stats(_write_chain(payloads))

        # The unfiltered numbers still see both calls...
        self.assertEqual(stats["return_stats"]["win"][2], 1)
        self.assertEqual(stats["return_stats"]["loss"][2], 1)
        # ...but the frozen-strategy-version slice sees neither: absent
        # is not the same as matching, and must not be reported as 0
        # calls averaging n/a in a way indistinguishable from "we checked
        # and there were none" versus "we couldn't tell." Both currently
        # print identically ("n=0 avg n/a"), which is the correct call --
        # the field's absence on old entries is documented, not a live
        # gap this run needs to explain -- so the test only pins the
        # count and n/a, not a separate "unknown" state.
        win_avg, win_median, win_n = stats["frozen_strategy_version_return_stats"]["win"]
        loss_avg, loss_median, loss_n = stats["frozen_strategy_version_return_stats"]["loss"]
        self.assertEqual(win_n, 0)
        self.assertIsNone(win_avg)
        self.assertEqual(loss_n, 0)
        self.assertIsNone(loss_avg)


class StrategyVersionFileHashTest(unittest.TestCase):
    """nightshift/strategy_version.py: a tooling-only change must leave
    strategy_version unchanged; a change to one of the surface files must
    move it. Builds a fake file tree under a temp dir mirroring
    STRATEGY_VERSION_FILES' relative paths plus one file that deliberately
    is NOT on the list, standing in for a tooling/infra file -- never
    touches the real nightshift/ source."""

    def _build_fake_repo(self, root: Path):
        for rel in STRATEGY_VERSION_FILES:
            p = root / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(f"# baseline content for {rel}\n")
        # Stands in for a tooling/infra file: real (e.g.
        # nightshift/anchor_ots.py), but deliberately absent from
        # STRATEGY_VERSION_FILES, so compute_strategy_version() never
        # reads it regardless of what's in it.
        tooling = root / "nightshift" / "anchor_ots.py"
        tooling.parent.mkdir(parents=True, exist_ok=True)
        tooling.write_text("# baseline tooling file\n")

    def test_tooling_only_change_leaves_strategy_version_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._build_fake_repo(root)
            baseline = compute_strategy_version(root=root)

            (root / "nightshift" / "anchor_ots.py").write_text(
                "# a completely different tooling fix, timeout handling etc.\n"
            )
            after = compute_strategy_version(root=root)

        self.assertEqual(baseline, after)

    def test_strategy_file_change_moves_strategy_version(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._build_fake_repo(root)
            baseline = compute_strategy_version(root=root)

            # Touch exactly one in-surface file (an arbitrary pick from
            # STRATEGY_VERSION_FILES -- any one of the seven must move it).
            target = root / sorted(STRATEGY_VERSION_FILES)[0]
            target.write_text(target.read_text() + "# a real parameter change\n")
            after = compute_strategy_version(root=root)

        self.assertNotEqual(baseline, after)


class ReceiptVerificationTest(unittest.TestCase):
    """Task 3: a missing or invalid OTS receipt must be visible in
    compute_stats()'s output (what main() prints from), not silently
    passed over -- and pre-anchoring entries must not count as gaps."""

    def _write_receipt(self, ots_dir, entry_hash, *, ok=True, magic=None,
                        sidecar=None):
        ots_dir.mkdir(parents=True, exist_ok=True)
        (ots_dir / f"{entry_hash}.hash").write_text(
            entry_hash if sidecar is None else sidecar
        )
        content = OTS_MAGIC + b"\x00" * 20 if ok else (magic or b"not-an-ots-file-at-all-")
        (ots_dir / f"{entry_hash}.hash.ots").write_bytes(content)

    def test_receipted_missing_invalid_all_counted_and_listed(self):
        with tempfile.TemporaryDirectory() as tmp:
            ots_dir = Path(tmp) / "ots"
            payloads = [{"type": "X", "n": 1}, {"type": "X", "n": 2}, {"type": "X", "n": 3}]
            chain_path = _write_chain(payloads, published_at_utc=POST_ANCHORING_DATE)
            hashes = [json.loads(l)["entry_hash"] for l in chain_path.read_text().splitlines()]

            self._write_receipt(ots_dir, hashes[0], ok=True)
            # hashes[1]: no receipt written at all -> missing
            self._write_receipt(ots_dir, hashes[2], ok=False)  # bad magic -> invalid

            stats = compute_stats(chain_path, ots_dir=ots_dir)

        self.assertEqual(stats["receipt_counts"],
                          {"receipted": 1, "missing": 1, "invalid": 1})
        self.assertEqual(stats["missing_receipt_entries"], [2])
        self.assertEqual(stats["invalid_receipt_entries"], [3])
        self.assertEqual(stats["predates_anchoring"], 0)

    def test_mismatched_sidecar_is_invalid_not_receipted(self):
        """A .hash file that exists but doesn't match the entry's own
        hash is corruption, not absence -- must not read as a pass."""
        with tempfile.TemporaryDirectory() as tmp:
            ots_dir = Path(tmp) / "ots"
            chain_path = _write_chain([{"type": "X"}], published_at_utc=POST_ANCHORING_DATE)
            entry_hash = json.loads(chain_path.read_text())["entry_hash"]
            self._write_receipt(ots_dir, entry_hash, ok=True, sidecar="wrong-hash-entirely")

            stats = compute_stats(chain_path, ots_dir=ots_dir)

        self.assertEqual(stats["receipt_counts"]["invalid"], 1)
        self.assertEqual(stats["receipt_counts"]["receipted"], 0)

    def test_pre_anchoring_entries_excluded_not_flagged_as_gaps(self):
        """No backfill: an entry from before ANCHORING_START has no
        receipt by design and must not show up as missing/invalid."""
        with tempfile.TemporaryDirectory() as tmp:
            ots_dir = Path(tmp) / "ots"  # left empty -- no receipts anywhere
            payloads = [{"type": "X", "n": 1}, {"type": "X", "n": 2}]
            chain_path = _write_chain(
                payloads, published_at_utc=[PRE_ANCHORING_DATE, POST_ANCHORING_DATE]
            )
            hashes = [json.loads(l)["entry_hash"] for l in chain_path.read_text().splitlines()]
            self._write_receipt(ots_dir, hashes[1], ok=True)  # only the post-anchoring one

            stats = compute_stats(chain_path, ots_dir=ots_dir)

        self.assertEqual(stats["predates_anchoring"], 1)
        self.assertEqual(stats["receipt_counts"],
                          {"receipted": 1, "missing": 0, "invalid": 0})
        self.assertEqual(stats["missing_receipt_entries"], [])

    def test_real_ots_magic_constant_matches_a_real_receipt_in_this_repo(self):
        """Not just internally consistent -- OTS_MAGIC must match what
        nightshift/anchor_ots.py actually produces. Skips gracefully if
        this checkout has no real receipts yet (e.g. ots was never
        installed here) rather than failing on an environment gap."""
        real_ots_dir = ROOT / "reports" / "ots"
        real_files = list(real_ots_dir.glob("*.hash.ots")) if real_ots_dir.exists() else []
        if not real_files:
            self.skipTest("no real .hash.ots files in this checkout to check against")
        header = real_files[0].read_bytes()[:len(OTS_MAGIC)]
        self.assertEqual(header, OTS_MAGIC)


if __name__ == "__main__":
    unittest.main()
