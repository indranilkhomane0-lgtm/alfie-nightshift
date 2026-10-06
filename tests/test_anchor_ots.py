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
        self._patches = [
            mock.patch.object(anchor_ots, "OTS_DIR", self.ots_dir),
            mock.patch.object(anchor_ots, "LOG_PATH", self.ots_dir / "anchor_ots.log"),
            mock.patch.object(anchor_ots, "UPGRADE_TOTAL_BUDGET_S", 30),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self._tmp.cleanup()

    def _make_proof(self, name: str) -> Path:
        f = self.ots_dir / name
        f.write_bytes(b"fake-proof-content")
        return f

    def _log_text(self) -> str:
        path = self.ots_dir / "anchor_ots.log"
        return path.read_text() if path.exists() else ""

    def test_already_confirmed_proof_is_skipped_not_reupgraded(self):
        """The core regression test for the bug: a proof whose `ots info`
        already shows a confirmed Bitcoin attestation must be skipped --
        `ots upgrade` must never even be called on it. Previously the
        skip-check never fired for ANY file, confirmed or not."""
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
