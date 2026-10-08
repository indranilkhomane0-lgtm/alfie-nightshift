#!/usr/bin/env python3
"""
Tests for nightshift/anchor_ots.py's opportunistic_upgrade() -- the
nightly backlog sweep that tries to complete still-pending OTS proofs.

Covers the fix for the skip-check defect found on the live chain
2026-10-06: "PendingAttestation" is a substring of `ots info`'s output
for EVERY real proof forever, confirmed or not (an upgrade only adds a
confirmed attestation leaf, it never removes the original pending one),
so checking for its absence never skipped anything -- every run
re-attempted the full candidate list from scratch, oldest-mtime first,
and the budget got spent re-walking the already-confirmed prefix before
ever reaching a real pending entry. 193 of 336 real receipts were stuck
pending as a result, 182 of them >48h old. The fix checks for the
presence of a CONFIRMED attestation instead.

All subprocess calls (the `ots` binary itself) are mocked -- these tests
never shell out to a real `ots` CLI or hit a real calendar server.

stdlib only. Run directly:

    python3 tests/test_anchor_ots.py
"""
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from nightshift import anchor_ots  # noqa: E402

OTS_BIN = "/usr/bin/true"  # never actually invoked -- subprocess.run is mocked


def _info_result(stdout: str) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=["ots", "info"], returncode=0, stdout=stdout, stderr="")


def _upgrade_result(returncode: int, stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=["ots", "upgrade"], returncode=returncode, stdout="", stderr=stderr)


PENDING_INFO = "verify PendingAttestation('https://bob.btc.calendar.opentimestamps.org')\n"
CONFIRMED_INFO = (
    "verify PendingAttestation('https://bob.btc.calendar.opentimestamps.org')\n"
    "verify BitcoinBlockHeaderAttestation(959951)\n"
)


class OpportunisticUpgradeTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.ots_dir = Path(self._tmp.name)
        self.index_path = self.ots_dir / "ots_confirmed.index"
        self._patches = [
            mock.patch.object(anchor_ots, "OTS_DIR", self.ots_dir),
            mock.patch.object(anchor_ots, "LOG_PATH", self.ots_dir / "anchor_ots.log"),
            mock.patch.object(anchor_ots, "CONFIRMED_INDEX_PATH", self.index_path),
            mock.patch.object(anchor_ots, "UPGRADE_TOTAL_BUDGET_S", 30),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self._tmp.cleanup()

    def _make_proof(self, name: str, mtime: float | None = None) -> Path:
        f = self.ots_dir / name
        f.write_bytes(b"fake-proof-content")
        if mtime is not None:
            import os
            os.utime(f, (mtime, mtime))
        return f

    def _log_text(self) -> str:
        path = self.ots_dir / "anchor_ots.log"
        return path.read_text() if path.exists() else ""

    def _index_contents(self) -> set:
        if not self.index_path.exists():
            return set()
        return {l.strip() for l in self.index_path.read_text().splitlines() if l.strip()}

    def test_already_confirmed_proof_is_skipped_not_reupgraded(self):
        """The core regression test for the 2026-10-06 bug: a proof whose
        `ots info` already shows a confirmed Bitcoin attestation must be
        skipped -- `ots upgrade` must never even be called on it. This is
        its FIRST encounter (not yet in the index), so one `ots info`
        call is expected; it must also get indexed as a result."""
        f = self._make_proof("confirmed.hash.ots")
        upgrade_calls = []

        def fake_run(args, **kwargs):
            if args[1] == "info":
                return _info_result(CONFIRMED_INFO)
            upgrade_calls.append(args)
            raise AssertionError("ots upgrade must not be called on an already-confirmed proof")

        with mock.patch("subprocess.run", side_effect=fake_run):
            anchor_ots.opportunistic_upgrade(OTS_BIN)

        self.assertEqual(upgrade_calls, [])
        self.assertEqual(self._index_contents(), {"confirmed"})

    def test_second_run_skips_indexed_entry_with_zero_subprocess_calls(self):
        """The actual throughput fix, 2026-10-08: once a hash is in the
        confirmed index, a LATER run must not call `ots info` on it at
        all -- not even the cheap, local, no-network call. This is what
        makes the sweep linear in pending receipts, not chain length."""
        self._make_proof("confirmed.hash.ots")
        with mock.patch("subprocess.run", return_value=_info_result(CONFIRMED_INFO)):
            anchor_ots.opportunistic_upgrade(OTS_BIN)  # first run: indexes it
        self.assertEqual(self._index_contents(), {"confirmed"})

        with mock.patch("subprocess.run") as m:
            anchor_ots.opportunistic_upgrade(OTS_BIN)  # second run
        m.assert_not_called()

    def test_newly_upgraded_entry_is_appended_to_index(self):
        self._make_proof("pending.hash.ots")

        def fake_run(args, **kwargs):
            if args[1] == "info":
                return _info_result(PENDING_INFO)
            return _upgrade_result(0)

        with mock.patch("subprocess.run", side_effect=fake_run):
            anchor_ots.opportunistic_upgrade(OTS_BIN)

        self.assertEqual(self._index_contents(), {"pending"})

    def test_unupgraded_entry_is_not_indexed(self):
        """A proof that stays pending must NOT be indexed -- only an
        actually-confirmed result (via info or a successful upgrade)
        ever gets appended. Indexing a still-pending hash would make
        this run's own 'not yet' permanent, silently."""
        self._make_proof("stuck.hash.ots")

        def fake_run(args, **kwargs):
            if args[1] == "info":
                return _info_result(PENDING_INFO)
            return _upgrade_result(1, stderr="Not yet confirmed in Bitcoin blockchain")

        with mock.patch("subprocess.run", side_effect=fake_run):
            anchor_ots.opportunistic_upgrade(OTS_BIN)

        self.assertEqual(self._index_contents(), set())

    def test_missing_index_treated_as_empty_nothing_pre_skipped(self):
        self.assertFalse(self.index_path.exists())
        self._make_proof("a.hash.ots")
        calls = []
        with mock.patch("subprocess.run", side_effect=lambda *a, **kw: (
            calls.append(1), _info_result(CONFIRMED_INFO))[1]):
            anchor_ots.opportunistic_upgrade(OTS_BIN)
        self.assertEqual(len(calls), 1, "a missing index must not be treated as 'everything confirmed'")

    def test_corrupt_index_is_treated_as_empty_and_rebuilt_not_raised(self):
        """'rebuildable from the receipts themselves if lost or
        corrupted' -- a garbled index file must not raise, and must not
        be trusted; it degrades to checking every candidate fresh, same
        as a missing index, and gets correct entries appended as they're
        discovered this run."""
        self.ots_dir.mkdir(parents=True, exist_ok=True)
        # Simulate unreadable content via a directory where a file is
        # expected -- read_text() raises IsADirectoryError.
        self.index_path.mkdir()
        self._make_proof("a.hash.ots")

        with mock.patch("subprocess.run", return_value=_info_result(CONFIRMED_INFO)):
            anchor_ots.opportunistic_upgrade(OTS_BIN)  # must not raise

        self.assertIn("ALERT", self._log_text())
        self.assertIn("rebuilding", self._log_text())

    def test_unindexed_candidates_are_tried_newest_first(self):
        """On a cold index (or right after it's lost), the newest files
        -- genuinely pending, by construction -- must be tried before
        the oldest, already-long-confirmed ones. Oldest-first would
        reproduce the 2026-10-08 bug on every cold start: budget spent
        re-indexing an old backlog before ever reaching what's actually
        pending."""
        self._make_proof("old.hash.ots", mtime=1000.0)
        self._make_proof("new.hash.ots", mtime=9000.0)
        order = []

        def fake_run(args, **kwargs):
            order.append(Path(args[2]).name)
            return _info_result(CONFIRMED_INFO)

        with mock.patch("subprocess.run", side_effect=fake_run):
            anchor_ots.opportunistic_upgrade(OTS_BIN)

        self.assertEqual(order, ["new.hash.ots", "old.hash.ots"])

    def test_indexed_entries_are_excluded_before_sorting_unindexed_ones(self):
        """A mix: one old entry already indexed (must cost nothing), one
        new entry not yet indexed (must be checked). The indexed one
        must never reach subprocess.run regardless of its age."""
        self._make_proof("old_confirmed.hash.ots", mtime=1000.0)
        self._make_proof("new_pending.hash.ots", mtime=9000.0)
        self.index_path.write_text("old_confirmed\n")

        calls = []
        def fake_run(args, **kwargs):
            calls.append(Path(args[2]).name)
            return _info_result(PENDING_INFO)

        with mock.patch("subprocess.run", side_effect=fake_run):
            anchor_ots.opportunistic_upgrade(OTS_BIN)

        # new_pending gets both an info check and an upgrade attempt
        # (still pending -> real work); old_confirmed, already indexed,
        # must never appear at all, at either call site.
        self.assertEqual(calls, ["new_pending.hash.ots", "new_pending.hash.ots"])

    def test_pending_proof_is_attempted_and_success_is_logged(self):
        """A genuinely still-pending proof must be attempted. On success
        (ots upgrade exits 0), the file is left for `ots upgrade` to have
        written back in place, and the result is logged as confirmed."""
        self._make_proof("pending.hash.ots")

        def fake_run(args, **kwargs):
            if args[1] == "info":
                return _info_result(PENDING_INFO)
            self.assertEqual(args[1], "upgrade")
            return _upgrade_result(0)

        with mock.patch("subprocess.run", side_effect=fake_run):
            anchor_ots.opportunistic_upgrade(OTS_BIN)

        self.assertIn("upgraded pending.hash.ots -- now Bitcoin-confirmed", self._log_text())

    def test_unupgradeable_receipt_stays_pending_without_crashing(self):
        """A proof that `ots upgrade` cannot yet complete (no calendar has
        a Bitcoin attestation ready) is normal, not an error: must not
        raise, must not delete the proof file, logs as still-pending."""
        f = self._make_proof("stuck.hash.ots")

        def fake_run(args, **kwargs):
            if args[1] == "info":
                return _info_result(PENDING_INFO)
            return _upgrade_result(1, stderr="Not yet confirmed in Bitcoin blockchain")

        with mock.patch("subprocess.run", side_effect=fake_run):
            anchor_ots.opportunistic_upgrade(OTS_BIN)  # must not raise

        self.assertTrue(f.exists(), "a receipt that can't yet upgrade must never be deleted")
        self.assertIn("upgrade not yet complete for stuck.hash.ots", self._log_text())

    def test_calendar_unreachable_is_caught_logged_and_sweep_continues(self):
        """An unreachable calendar (timeout, DNS failure, etc.) during the
        upgrade attempt itself must be caught -- never raise, never abort
        the rest of the sweep -- same treatment as every other network
        path in this repo: defer, log, retry next run."""
        self._make_proof("a_unreachable.hash.ots")
        self._make_proof("b_should_still_run.hash.ots")
        attempted_upgrades = []

        def fake_run(args, **kwargs):
            if args[1] == "info":
                return _info_result(PENDING_INFO)
            attempted_upgrades.append(Path(args[2]).name)
            raise subprocess.TimeoutExpired(cmd=args, timeout=25)

        with mock.patch("subprocess.run", side_effect=fake_run):
            anchor_ots.opportunistic_upgrade(OTS_BIN)  # must not raise

        self.assertEqual(sorted(attempted_upgrades),
                         ["a_unreachable.hash.ots", "b_should_still_run.hash.ots"],
                         "one unreachable calendar must not stop the sweep from trying the next file")
        log = self._log_text()
        self.assertIn("a_unreachable.hash.ots", log)
        self.assertIn("raised", log)

    def test_info_call_raising_is_treated_as_skip_not_crash(self):
        """If even `info` (a purely local, offline parse) fails -- a
        corrupted file, a race with a concurrent writer -- skip that one
        file and move on rather than crashing the whole sweep."""
        self._make_proof("corrupt.hash.ots")
        self._make_proof("fine.hash.ots")
        upgrade_calls = []

        def fake_run(args, **kwargs):
            if args[1] == "info":
                if "corrupt" in args[2]:
                    raise Exception("truncated file")
                return _info_result(PENDING_INFO)
            upgrade_calls.append(Path(args[2]).name)
            return _upgrade_result(0)

        with mock.patch("subprocess.run", side_effect=fake_run):
            anchor_ots.opportunistic_upgrade(OTS_BIN)  # must not raise

        self.assertEqual(upgrade_calls, ["fine.hash.ots"])

    def test_budget_exhaustion_stops_sweep_and_logs(self):
        """Once the total time budget is used up, remaining candidates are
        left for the next run -- never attempted past the cutoff."""
        self._make_proof("x1.hash.ots")
        self._make_proof("x2.hash.ots")

        times = iter([0.0, 0.0, 100.0])  # start, first check (inside budget), second check (over budget)

        def fake_run(args, **kwargs):
            if args[1] == "info":
                return _info_result(PENDING_INFO)
            return _upgrade_result(0)

        with mock.patch("subprocess.run", side_effect=fake_run), \
             mock.patch("time.monotonic", side_effect=lambda: next(times, 100.0)):
            anchor_ots.opportunistic_upgrade(OTS_BIN)

        self.assertIn("upgrade sweep hit", self._log_text())
        self.assertIn("budget", self._log_text())


class MainMissingOtsBinaryTest(unittest.TestCase):
    """main()'s guard for the `ots` CLI not being installed at all --
    must exit 0 (never block the nightly run) and log plainly, not stamp
    or upgrade anything."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "chain.jsonl").write_text('{"entry_hash": "a"}\n')
        self._patches = [
            mock.patch.object(anchor_ots, "CHAIN_PATH", self.root / "chain.jsonl"),
            mock.patch.object(anchor_ots, "OTS_DIR", self.root / "ots"),
            mock.patch.object(anchor_ots, "LOG_PATH", self.root / "anchor_ots.log"),
            mock.patch.object(anchor_ots, "find_ots", return_value=None),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self._tmp.cleanup()

    def test_missing_binary_exits_clean_and_logs(self):
        rc = anchor_ots.main()
        self.assertEqual(rc, 0)
        log = (self.root / "anchor_ots.log").read_text()
        self.assertIn("ots CLI not installed", log)
        self.assertFalse((self.root / "ots").exists())


if __name__ == "__main__":
    unittest.main()
