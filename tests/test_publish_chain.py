#!/usr/bin/env python3
"""
Tests for nightshift/publish_chain.py's publish-time OpenTimestamps
stamping (_stamp_at_publish_time) and its defer-to-anchor_ots.py's-sweep
behavior on a calendar failure -- the OTS equivalent of run_and_publish.sh's
push_or_defer() and its "more than one deferred commit" test, but for
anchoring instead of git push.

Forces the unreachable-calendar condition by monkeypatching
nightshift.anchor_ots.stamp() (the one function that actually shells out
to the `ots` CLI) rather than hitting a real calendar server or faking a
whole `ots` binary -- anchor_ots.stamp()'s own subprocess/timeout handling
already has dedicated coverage from when it was fixed; what's under test
here is what publish_chain.py and anchor_ots.py's sweep do around that
boundary, not the boundary itself.

stdlib only. Run directly:

    python3 tests/test_publish_chain.py
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from nightshift import anchor_ots  # noqa: E402
from nightshift.publish_chain import _stamp_at_publish_time, OTS_DIR as PUBLISH_OTS_DIR  # noqa: E402


class StampAtPublishTimeTest(unittest.TestCase):
    """_stamp_at_publish_time(): best-effort, single attempt, never raises,
    never leaves an orphaned .hash without a .hash.ots."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.ots_dir = Path(self._tmp.name)
        # publish_chain.py's OTS_DIR is a module-level constant computed at
        # import time -- point it at our sandbox for the duration of each
        # test, restore after. Never touches the real reports/ots/.
        self._patch = mock.patch("nightshift.publish_chain.OTS_DIR", self.ots_dir)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        self._tmp.cleanup()

    def test_ots_not_installed_defers_silently(self):
        with mock.patch.object(anchor_ots, "find_ots", return_value=None):
            result = _stamp_at_publish_time("deadbeef" * 8)
        self.assertFalse(result)
        self.assertEqual(list(self.ots_dir.glob("*")), [])

    def test_calendar_unreachable_defers_and_leaves_no_orphan_hash(self):
        """The failure mode this test forces: find_ots() succeeds (ots IS
        installed) but stamp() fails, exactly as it does on a real
        unreachable-calendar timeout (see anchor_ots.stamp()'s own
        try/except around the subprocess call). Must not raise, must not
        leave a .hash file with no matching .hash.ots -- that combination
        is the crash-mid-stamp signature self_audit's orphan_hash_no_ots
        check flags, and _stamp_at_publish_time is responsible for not
        creating it even on a clean failure."""
        with mock.patch.object(anchor_ots, "find_ots", return_value="/usr/bin/true"), \
             mock.patch.object(anchor_ots, "stamp", return_value=False):
            result = _stamp_at_publish_time("cafef00d" * 8)
        self.assertFalse(result)
        self.assertEqual(list(self.ots_dir.glob("*")), [])

    def test_calendar_reachable_writes_both_files(self):
        def fake_stamp(ots_bin, hash_file):
            hash_file.with_suffix(".hash.ots").write_bytes(b"fake-but-present")
            return True
        with mock.patch.object(anchor_ots, "find_ots", return_value="/usr/bin/true"), \
             mock.patch.object(anchor_ots, "stamp", side_effect=fake_stamp):
            result = _stamp_at_publish_time("f00dcafe" * 8)
        self.assertTrue(result)
        self.assertTrue((self.ots_dir / ("f00dcafe" * 8 + ".hash")).exists())
        self.assertTrue((self.ots_dir / ("f00dcafe" * 8 + ".hash.ots")).exists())


class DeferAndSweepTest(unittest.TestCase):
    """The full pattern end to end: entries chained while the calendar is
    down get no receipt but ARE chained (publish never blocks); a later
    sweep, once the calendar is back, clears ALL of them in one pass --
    same shape as push_or_defer() clearing more than one deferred commit."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.chain_path = self.root / "chain.jsonl"
        self.ots_dir = self.root / "ots"
        self.chain_path.write_text("")
        # anchor_ots.py's unanchored_entries()/stamp path reads its own
        # module-level CHAIN_PATH/OTS_DIR -- point both at the sandbox.
        self._patches = [
            mock.patch("nightshift.publish_chain.OTS_DIR", self.ots_dir),
            mock.patch.object(anchor_ots, "CHAIN_PATH", self.chain_path),
            mock.patch.object(anchor_ots, "OTS_DIR", self.ots_dir),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self._tmp.cleanup()

    def _chain_entry(self, n):
        """Writes one entry directly (bypassing publish_chain.append_entry's
        code_version/predictions plumbing, irrelevant here) and calls
        _stamp_at_publish_time on it, exactly as append_entry() does."""
        import hashlib

        def canonical(o):
            return json.dumps(o, sort_keys=True, separators=(",", ":")).encode()

        lines = self.chain_path.read_text().splitlines()
        prev = json.loads(lines[-1])["entry_hash"] if lines else "0" * 64
        payload = {"type": "TEST", "n": n}
        entry_hash = hashlib.sha256(prev.encode() + canonical(payload)).hexdigest()
        entry = {
            "published_at_utc": "2026-09-16T00:00:00+00:00",
            "prev_hash": prev, "payload": payload, "entry_hash": entry_hash,
        }
        with self.chain_path.open("a") as f:
            f.write(json.dumps(entry, sort_keys=True) + "\n")
        _stamp_at_publish_time(entry_hash)
        return entry_hash

    def test_two_entries_deferred_then_one_sweep_clears_both(self):
        # Calendar down for both publishes -- same forced failure as above.
        with mock.patch.object(anchor_ots, "find_ots", return_value="/usr/bin/true"), \
             mock.patch.object(anchor_ots, "stamp", return_value=False):
            h1 = self._chain_entry(1)
            h2 = self._chain_entry(2)

        self.assertFalse((self.ots_dir / f"{h1}.hash.ots").exists())
        self.assertFalse((self.ots_dir / f"{h2}.hash.ots").exists())

        # Calendar back up. One sweep call, same as anchor_ots.py's own
        # main() calls unanchored_entries() + stamps each -- must clear
        # BOTH outstanding entries, not just the newest.
        def fake_stamp(ots_bin, hash_file):
            hash_file.with_suffix(".hash.ots").write_bytes(b"fake-but-present")
            return True
        with mock.patch.object(anchor_ots, "stamp", side_effect=fake_stamp):
            pending = anchor_ots.unanchored_entries()
            self.assertEqual(len(pending), 2, "both deferred entries should be pending")
            for entry_hash in pending:
                hash_file = self.ots_dir / f"{entry_hash}.hash"
                hash_file.write_text(entry_hash)
                anchor_ots.stamp("/usr/bin/true", hash_file)

        self.assertTrue((self.ots_dir / f"{h1}.hash.ots").exists())
        self.assertTrue((self.ots_dir / f"{h2}.hash.ots").exists())
        self.assertEqual(anchor_ots.unanchored_entries(), [],
                          "sweep should have cleared the backlog completely")


if __name__ == "__main__":
    unittest.main()
