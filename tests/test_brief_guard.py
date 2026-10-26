#!/usr/bin/env python3
"""
Tests for nightshift/brief_guard.py: an already-chained brief cannot be
rewritten -- not by a second cycle for the same cycle_id (the 2026-09-26
mechanism), and not by any other writer once publish_chain.py --brief
has marked it read-only.

Run directly:

    python3 tests/test_brief_guard.py
"""
import hashlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from nightshift import brief_guard  # noqa: E402


def _chain_with_brief(path: Path, name: str) -> None:
    entry = {"payload": {"type": "NIGHTLY_BRIEF", "brief_file": name,
                         "brief_sha256": "0" * 64}}
    path.write_text(json.dumps(entry) + "\n")


class BriefGuardTest(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.chain = self.root / "chain.jsonl"
        p = mock.patch.object(brief_guard, "CHAIN_PATH", self.chain)
        p.start()
        self.addCleanup(p.stop)
        self.addCleanup(self._tmp.cleanup)

    def test_unchained_brief_can_be_written_and_rewritten(self):
        """A brief that never reached the chain (e.g. publish failed and
        the night is retried) is not final yet."""
        path = self.root / "brief_20261027.txt"
        brief_guard.write_brief(path, "first")
        brief_guard.write_brief(path, "second")
        self.assertEqual(path.read_text(), "second")

    def test_chained_brief_is_refused_and_left_intact(self):
        path = self.root / "brief_20260926.txt"
        path.write_text("original")
        _chain_with_brief(self.chain, path.name)
        with self.assertRaises(brief_guard.BriefAlreadyChained):
            brief_guard.write_brief(path, "overwrite")
        self.assertEqual(path.read_text(), "original")

    def test_no_chain_file_means_nothing_chained(self):
        self.assertEqual(brief_guard.chained_brief_files(), set())

    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root ignores modes")
    def test_read_only_mark_blocks_any_other_writer(self):
        path = self.root / "brief_20261027.txt"
        path.write_text("published")
        brief_guard.mark_read_only(path)
        with self.assertRaises(PermissionError):
            path.write_text("overwrite")
        self.assertEqual(path.read_text(), "published")
        os.chmod(path, 0o644)  # let TemporaryDirectory clean up

    def test_real_chain_names_every_real_brief(self):
        real = brief_guard.chained_brief_files(ROOT / "reports" / "chain.jsonl")
        if not real:
            self.skipTest("no real chain in this checkout")
        self.assertIn("brief_20260926.txt", real)


class CycleRefusesChainedBriefTest(unittest.TestCase):
    """The real NightShiftCycle.run() must refuse before any side effect
    when its brief is already chained -- the second 2026-09-26 run would
    have stopped here instead of rewriting brief_20260926.txt."""

    @classmethod
    def setUpClass(cls):
        try:
            import pandas  # noqa: F401
            import sklearn  # noqa: F401
            import hmmlearn  # noqa: F401
        except ImportError as exc:
            raise unittest.SkipTest(f"full pipeline deps not importable: {exc}")

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.chain = root / "chain.jsonl"
        self.brief_dir = root / "briefs"
        self.brief_dir.mkdir()
        from nightshift import cycle, db as nsdb, meta_model
        self.cycle = cycle
        self._patches = [
            mock.patch.object(brief_guard, "CHAIN_PATH", self.chain),
            mock.patch.object(cycle, "BRIEF_DIR", self.brief_dir),
            mock.patch.object(nsdb, "DB_PATH", root / "corpus.db"),
            mock.patch.object(meta_model, "MODEL_PATH", root / "meta_model.pkl"),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self._tmp.cleanup()

    def test_second_cycle_for_a_chained_night_refuses_before_side_effects(self):
        brief = self.brief_dir / "brief_99999902.txt"
        brief.write_text("published")
        _chain_with_brief(self.chain, brief.name)
        sha_before = hashlib.sha256(brief.read_bytes()).hexdigest()
        with mock.patch.object(self.cycle, "init_db") as init_db, \
             mock.patch.object(self.cycle.NightShiftCycle, "_stages") as stages:
            night = self.cycle.NightShiftCycle(cycle_id=99999902)
            with self.assertRaises(brief_guard.BriefAlreadyChained):
                night.run()
        init_db.assert_not_called()
        stages.assert_not_called()
        self.assertEqual(hashlib.sha256(brief.read_bytes()).hexdigest(), sha_before)


if __name__ == "__main__":
    unittest.main()
