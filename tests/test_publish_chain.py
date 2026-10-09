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
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
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

    def test_ohlcv_archive_falls_under_the_same_reports_archive_exclusion(self):
        """nightshift/archive_ohlcv.py writes reports/archive/ohlcv.csv.gz
        -- same directory the corpus-delta archive already lives in and
        already excludes. No separate entry in RUN_OUTPUT_PATHS was
        added for it, by design: ':!reports/archive' is a directory
        exclusion, not a file exclusion, so it already covers any file
        written under that path. Confirmed here, not just assumed."""
        (self.root / "reports" / "archive" / "ohlcv.csv.gz").write_bytes(b"\x1f\x8b\x00fake-gzip")
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


class OhlcvArchiveSnapshotTest(unittest.TestCase):
    """_ohlcv_archive_snapshot()'s (row count, sha256, read_error) for
    the raw OHLCV archive -- same reference-by-hash pattern as
    _corpus_delta_snapshot()/_predictions_snapshot(), with one
    difference: the file is gzip, so the sha256 is over the raw
    (compressed) bytes actually committed, and the row count comes from
    decompressing first."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.archive_path = Path(self._tmp.name) / "ohlcv.csv.gz"
        self._patch = mock.patch.object(publish_chain, "OHLCV_ARCHIVE_PATH", self.archive_path)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        self._tmp.cleanup()

    def test_missing_file_reports_none_not_zero(self):
        n, sha, err = publish_chain._ohlcv_archive_snapshot()
        self.assertIsNone(n)
        self.assertIsNone(sha)
        self.assertIsNotNone(err)

    def test_present_file_decompresses_for_count_hashes_raw_bytes(self):
        import gzip, hashlib
        with gzip.open(self.archive_path, "wb") as f:
            f.write(b"2026-10-07,BTC/USDT,2026-10-06T00:00:00,binance,1.0,2.0,0.5,1.5,100.0\n")
        with gzip.open(self.archive_path, "ab") as f:  # a second, appended member
            f.write(b"2026-10-07,ETH/USDT,2026-10-06T00:00:00,binance,1.0,2.0,0.5,1.5,100.0\n")

        n, sha, err = publish_chain._ohlcv_archive_snapshot()
        self.assertEqual(n, 2, "row count must reflect BOTH concatenated gzip members")
        self.assertEqual(sha, hashlib.sha256(self.archive_path.read_bytes()).hexdigest(),
                          "hash must be over the raw file bytes, not the decompressed content")
        self.assertIsNone(err)

    def test_corrupt_gzip_reports_read_error_not_raise(self):
        self.archive_path.write_bytes(b"not actually gzip data")
        n, sha, err = publish_chain._ohlcv_archive_snapshot()
        self.assertIsNone(n)
        self.assertIsNone(sha)
        self.assertIsNotNone(err)

    def test_append_entry_includes_ohlcv_archive_fields(self):
        import gzip
        with gzip.open(self.archive_path, "wb") as f:
            f.write(b"2026-10-07,BTC/USDT,2026-10-06T00:00:00,binance,1.0,2.0,0.5,1.5,100.0\n")
        with tempfile.TemporaryDirectory() as chain_tmp:
            chain_path = Path(chain_tmp) / "chain.jsonl"
            with mock.patch.object(publish_chain, "CHAIN_PATH", chain_path), \
                 mock.patch.object(publish_chain, "_stamp_at_publish_time", return_value=False):
                entry = publish_chain.append_entry({"type": "TEST"})
        self.assertEqual(entry["payload"]["ohlcv_archive_n"], 1)
        self.assertIsNotNone(entry["payload"]["ohlcv_archive_sha256"])
        self.assertNotIn("ohlcv_archive_read_error", entry["payload"])


class GraduationSidecarTest(unittest.TestCase):
    """_read_and_clear_graduation_sidecar()'s same-day/same-run binding --
    same discipline as the PIPELINE_FAILURE sidecar (chain entries
    285/286: a same-day-only check once misread a stale sidecar from an
    unrelated run), built in from this sidecar's first version rather
    than added after an incident. Unlike the failure sidecar, there is
    no fallback payload: any validation failure must yield None, never
    a best-guess dict, and must always delete the file either way."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.sidecar_path = Path(self._tmp.name) / "meta_model_graduation.json"
        self._patch = mock.patch.object(publish_chain, "GRADUATION_SIDECAR_PATH", self.sidecar_path)
        self._patch.start()
        self._env_patch = mock.patch.dict(os.environ, {"ALFIE_RUN_ID": "run-123"})
        self._env_patch.start()

    def tearDown(self):
        self._env_patch.stop()
        self._patch.stop()
        self._tmp.cleanup()

    def _write(self, **overrides):
        payload = {
            "cycle_id": 42, "date": "2026-10-09", "first_training": True,
            "n_train": 30, "cv_auc": 0.71, "cv_r2": 0.22,
            "gate_counter_name": "labeled_prediction_date_count",
            "gate_counter_value": 30, "feature_importances": {"wfo_sharpe": 0.4},
            "written_at_utc": datetime.now(timezone.utc).isoformat(),
            "run_id": "run-123",
        }
        payload.update(overrides)
        self.sidecar_path.write_text(json.dumps(payload))

    def test_missing_sidecar_returns_none(self):
        self.assertIsNone(publish_chain._read_and_clear_graduation_sidecar())

    def test_valid_same_day_same_run_returns_dict(self):
        self._write()
        grad = publish_chain._read_and_clear_graduation_sidecar()
        self.assertIsNotNone(grad)
        self.assertEqual(grad["cycle_id"], 42)
        self.assertEqual(grad["gate_counter_name"], "labeled_prediction_date_count")

    def test_valid_sidecar_is_deleted_after_read(self):
        self._write()
        publish_chain._read_and_clear_graduation_sidecar()
        self.assertFalse(self.sidecar_path.exists())

    def test_stale_date_rejected_and_still_deleted(self):
        stale = datetime.now(timezone.utc) - timedelta(days=1)
        self._write(written_at_utc=stale.isoformat())
        self.assertIsNone(publish_chain._read_and_clear_graduation_sidecar())
        self.assertFalse(self.sidecar_path.exists())

    def test_different_run_id_rejected_and_still_deleted(self):
        self._write(run_id="some-other-run")
        self.assertIsNone(publish_chain._read_and_clear_graduation_sidecar())
        self.assertFalse(self.sidecar_path.exists())

    def test_both_sides_unknown_rejected(self):
        self._env_patch.stop()
        self._env_patch = mock.patch.dict(os.environ, {}, clear=True)
        self._env_patch.start()
        self._write(run_id="unknown")
        self.assertIsNone(publish_chain._read_and_clear_graduation_sidecar())

    def test_malformed_json_rejected_and_still_deleted(self):
        self.sidecar_path.write_text("{not json")
        self.assertIsNone(publish_chain._read_and_clear_graduation_sidecar())
        self.assertFalse(self.sidecar_path.exists())


class GraduationCliTest(unittest.TestCase):
    """main()'s --graduation dispatch end to end: a valid sidecar produces
    a chained METHODOLOGY_CHANGE entry carrying the gate/training fields;
    no sidecar publishes nothing and exits 0, same shape as a quiet night
    for every other conditional entry type in this script."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.chain_path = self.root / "chain.jsonl"
        self.sidecar_path = self.root / "meta_model_graduation.json"
        self._patches = [
            mock.patch.object(publish_chain, "CHAIN_PATH", self.chain_path),
            mock.patch.object(publish_chain, "GRADUATION_SIDECAR_PATH", self.sidecar_path),
            mock.patch.object(publish_chain, "_stamp_at_publish_time", return_value=False),
            mock.patch.dict(os.environ, {"ALFIE_RUN_ID": "run-abc"}),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self._tmp.cleanup()

    def test_valid_sidecar_publishes_methodology_change_with_gate_fields(self):
        self.sidecar_path.write_text(json.dumps({
            "cycle_id": 7, "date": "2026-10-09", "first_training": True,
            "n_train": 30, "cv_auc": 0.65, "cv_r2": 0.15,
            "gate_counter_name": "labeled_prediction_date_count",
            "gate_counter_value": 30, "feature_importances": {"wfo_sharpe": 0.5},
            "written_at_utc": datetime.now(timezone.utc).isoformat(),
            "run_id": "run-abc",
        }))
        with mock.patch.object(sys, "argv", ["publish_chain.py", "--graduation"]):
            rc = publish_chain.main()
        self.assertEqual(rc, 0)
        lines = self.chain_path.read_text().splitlines()
        self.assertEqual(len(lines), 1)
        entry = json.loads(lines[0])
        self.assertEqual(entry["payload"]["type"], "METHODOLOGY_CHANGE")
        self.assertEqual(entry["payload"]["data"]["cycle_id"], 7)
        self.assertEqual(entry["payload"]["data"]["gate_counter_name"],
                          "labeled_prediction_date_count")
        self.assertEqual(entry["payload"]["data"]["n_train"], 30)
        self.assertFalse(self.sidecar_path.exists())

    def test_no_sidecar_publishes_nothing_and_exits_zero(self):
        with mock.patch.object(sys, "argv", ["publish_chain.py", "--graduation"]):
            rc = publish_chain.main()
        self.assertEqual(rc, 0)
        self.assertFalse(self.chain_path.exists())


if __name__ == "__main__":
    unittest.main()
