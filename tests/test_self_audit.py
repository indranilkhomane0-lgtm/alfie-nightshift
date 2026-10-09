#!/usr/bin/env python3
"""
Tests for nightshift/self_audit.py's check_doc_reference_integrity() --
specifically its KNOWN_TRANSIENT_ARTIFACT_PATHS allowlist, which fixes
the standing false positive on nightshift/logs/last_pipeline_failure.json
(a sidecar that legitimately doesn't exist most of the time, flagged as
"missing" every single quiet night). A real broken reference -- wrong
directory, not just "doesn't exist right now" -- must still be caught.

Builds a real throwaway repo-shaped sandbox (README.md + run_and_publish.sh)
and points self_audit.ROOT at it, same pattern
tests/test_publish_chain.py's CodeVersionDirtyScopeTest uses for
publish_chain.REPO_ROOT.

stdlib only. Run directly:

    python3 tests/test_self_audit.py
"""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from nightshift import self_audit  # noqa: E402


class DocReferenceIntegrityTransientArtifactTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "nightshift" / "logs").mkdir(parents=True)
        (self.root / "launchd").mkdir()
        self._patch = mock.patch.object(self_audit, "ROOT", self.root)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        self._tmp.cleanup()

    def _write_run_and_publish(self, text):
        (self.root / "run_and_publish.sh").write_text(text)
        (self.root / "README.md").write_text("# test\n")

    def test_known_transient_sidecar_absent_is_not_flagged(self):
        """The real fix: last_pipeline_failure.json doesn't exist (no
        failure tonight) but its parent dir (nightshift/logs/) does --
        this must read as normal, not as a broken doc reference."""
        self._write_run_and_publish(
            "# see nightshift/logs/last_pipeline_failure.json for detail\n")
        result = self_audit.check_doc_reference_integrity()
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(result["detail"]["missing_paths"], [])

    def test_known_transient_sidecar_with_missing_parent_dir_is_still_flagged(self):
        """A real typo in the cited path (wrong directory this time) must
        not be swallowed by the allowlist -- the allowlist only waives
        "doesn't exist right now", not "the directory itself is wrong"."""
        import shutil
        shutil.rmtree(self.root / "nightshift" / "logs")
        self._write_run_and_publish(
            "# see nightshift/logs/last_pipeline_failure.json for detail\n")
        result = self_audit.check_doc_reference_integrity()
        self.assertEqual(result["status"], "FAIL")
        cited = [m["cited"] for m in result["detail"]["missing_paths"]]
        self.assertIn("nightshift/logs/last_pipeline_failure.json", cited)

    def test_unrelated_missing_path_is_still_flagged(self):
        """Regression guard: the allowlist must not swallow everything --
        a genuinely broken, non-allowlisted reference still fails."""
        self._write_run_and_publish(
            "# see nightshift/run_nightshift.py for failure classification\n")
        result = self_audit.check_doc_reference_integrity()
        self.assertEqual(result["status"], "FAIL")
        cited = [m["cited"] for m in result["detail"]["missing_paths"]]
        self.assertIn("nightshift/run_nightshift.py", cited)

    def test_graduation_sidecar_also_allowlisted(self):
        self._write_run_and_publish(
            "# reads nightshift/logs/meta_model_graduation.json if present\n")
        result = self_audit.check_doc_reference_integrity()
        self.assertEqual(result["status"], "PASS")


if __name__ == "__main__":
    unittest.main()
