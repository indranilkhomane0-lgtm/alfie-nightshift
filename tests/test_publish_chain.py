#!/usr/bin/env python3
"""
Tests for nightshift/publish_chain.py's publish-time OpenTimestamps
stamping (_stamp_at_publish_time) and its defer-to-anchor_ots.py's-sweep
behavior on a calendar failure -- the OTS equivalent of run_and_publish.sh's
push_or_defer() and its "more than one deferred commit" test, but for
anchoring instead of git push -- plus _code_version()'s dirty-check
scoping (CodeVersionDirtyScopeTest).

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
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from nightshift import anchor_ots  # noqa: E402
from nightshift.publish_chain import _stamp_at_publish_time, OTS_DIR as PUBLISH_OTS_DIR  # noqa: E402
from nightshift import publish_chain  # noqa: E402


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


class CodeVersionDirtyScopeTest(unittest.TestCase):
    """_code_version()'s dirty check must report "a tracked source file
    has uncommitted changes", not "the run has written its own output
    files yet" -- the bug behind chain entries 288-332's code_dirty: true.
    Builds a real throwaway git repo (subprocess `git status` needs one)
    and points publish_chain.REPO_ROOT at it; never touches this repo."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        subprocess.run(["git", "init", "-q"], cwd=self.root, check=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"],
                        cwd=self.root, check=True)
        subprocess.run(["git", "config", "user.name", "test"],
                        cwd=self.root, check=True)
        # One tracked "source" file, committed, plus the run-output paths
        # exactly as RUN_OUTPUT_PATHS names them -- two are files
        # (chain.jsonl, predictions.jsonl), three are directories.
        (self.root / "nightshift").mkdir()
        (self.root / "nightshift" / "config.py").write_text("THRESHOLD = 1\n")
        (self.root / "reports").mkdir()
        for rel in publish_chain.RUN_OUTPUT_PATHS:
            path = self.root / rel
            if path.suffix:
                path.write_text("committed\n")
            else:
                path.mkdir(parents=True)
                (path / "placeholder").write_text("committed\n")
        subprocess.run(["git", "add", "-A"], cwd=self.root, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=self.root, check=True)

        self._patch = mock.patch.object(publish_chain, "REPO_ROOT", self.root)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        self._tmp.cleanup()
        publish_chain._code_version.cache_clear()

    def _dirty(self):
        # _code_version() is lru_cache(maxsize=1) -- per-process, by
        # design (the answer can't change mid-run). Each assertion here
        # represents a distinct simulated run, so clear it first.
        publish_chain._code_version.cache_clear()
        _, dirty = publish_chain._code_version()
        return dirty

    def test_clean_tree_is_not_dirty(self):
        self.assertFalse(self._dirty())

    def test_uncommitted_run_output_alone_is_not_dirty(self):
        """The exact scenario behind entries 288-332: tonight's brief,
        predictions.jsonl, chain.jsonl and OTS receipts written to disk,
        not yet staged/committed by run_and_publish.sh's end-of-run git
        add. Must now read as clean."""
        (self.root / "nightshift" / "briefs" / "brief_20270101.txt").write_text("brief\n")
        (self.root / "reports" / "chain.jsonl").write_text('{"n": 2}\n')
        (self.root / "reports" / "ots" / "new.hash").write_text("deadbeef\n")
        (self.root / "reports" / "audit" / "audit_20270101.json").write_text("{}\n")
        self.assertFalse(self._dirty())

    def test_uncommitted_source_change_is_dirty(self):
        """A real edit to a tracked file outside RUN_OUTPUT_PATHS must
        still trip dirty=True -- the fix narrows the check, it must not
        silence it. (Same defect class inverted, per the brief: a flag
        that's permanently false is as uninformative as one that's
        permanently true.)"""
        (self.root / "nightshift" / "config.py").write_text("THRESHOLD = 2\n")
        self.assertTrue(self._dirty())

    def test_untracked_source_file_is_dirty(self):
        """A new, never-committed source file (not just an edit to an
        existing one) must also trip dirty=True."""
        (self.root / "nightshift" / "new_module.py").write_text("x = 1\n")
        self.assertTrue(self._dirty())

    def test_source_change_alongside_run_output_is_still_dirty(self):
        """Mixed case: tonight's run output plus a genuine uncommitted
        source edit. The output half must not mask the source half."""
        (self.root / "reports" / "chain.jsonl").write_text('{"n": 2}\n')
        (self.root / "nightshift" / "config.py").write_text("THRESHOLD = 3\n")
        self.assertTrue(self._dirty())


class CorpusDeltaSnapshotTest(unittest.TestCase):
    """_corpus_delta_snapshot()'s (n, sha256, read_error) for the
    corpus-delta archive -- same reference-by-hash pattern as
    _predictions_snapshot(), including append_entry() wiring both
    corpus_delta_n and corpus_delta_sha256 into every chained payload."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.archive_path = Path(self._tmp.name) / "corpus_delta.jsonl"
        self._patch = mock.patch.object(publish_chain, "CORPUS_DELTA_PATH", self.archive_path)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        self._tmp.cleanup()

    def test_missing_file_reports_none_not_zero(self):
        """Absent entirely before this feature existed, or for a window
        after a write failure before the next sweep -- must read as
        (None, None, reason), never as (0, <hash-of-empty>, None), which
        would misreport 'never existed' as 'exists and is empty'."""
        n, sha, err = publish_chain._corpus_delta_snapshot()
        self.assertIsNone(n)
        self.assertIsNone(sha)
        self.assertIsNotNone(err)

    def test_present_file_hashes_and_counts_lines(self):
        import hashlib
        self.archive_path.write_text('{"kind":"cycle_marker"}\n{"kind":"candidate"}\n')
        n, sha, err = publish_chain._corpus_delta_snapshot()
        self.assertEqual(n, 2)
        self.assertEqual(sha, hashlib.sha256(self.archive_path.read_bytes()).hexdigest())
        self.assertIsNone(err)

    def test_append_entry_includes_corpus_delta_fields(self):
        """End to end through the real append_entry(), same as
        predictions_n/predictions_sha256 -- a reader of the chain gets
        both fields on every entry, not just NIGHTLY_BRIEF ones."""
        self.archive_path.write_text('{"kind":"cycle_marker"}\n')
        with tempfile.TemporaryDirectory() as chain_tmp:
            chain_path = Path(chain_tmp) / "chain.jsonl"
            with mock.patch.object(publish_chain, "CHAIN_PATH", chain_path), \
                 mock.patch.object(publish_chain, "_stamp_at_publish_time", return_value=False):
                entry = publish_chain.append_entry({"type": "TEST"})
        self.assertEqual(entry["payload"]["corpus_delta_n"], 1)
        self.assertIsNotNone(entry["payload"]["corpus_delta_sha256"])
        self.assertNotIn("corpus_delta_read_error", entry["payload"])

    def test_append_entry_missing_archive_sets_read_error_not_blocked(self):
        with tempfile.TemporaryDirectory() as chain_tmp:
            chain_path = Path(chain_tmp) / "chain.jsonl"
            with mock.patch.object(publish_chain, "CHAIN_PATH", chain_path), \
                 mock.patch.object(publish_chain, "_stamp_at_publish_time", return_value=False):
                entry = publish_chain.append_entry({"type": "TEST"})
        self.assertIsNone(entry["payload"]["corpus_delta_n"])
        self.assertIsNone(entry["payload"]["corpus_delta_sha256"])
        self.assertIn("corpus_delta_read_error", entry["payload"])


if __name__ == "__main__":
    unittest.main()
