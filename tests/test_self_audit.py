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


class AuditSuppressionTableTest(unittest.TestCase):
    """_audit_suppression_table()'s two self-checks: a suppressed symbol
    that no longer exists in the scanned source, and a DELIBERATELY_DEAD
    symbol that the checker now finds reachable (it may have gained a
    real caller) -- both must surface as findings, not be silently
    trusted. The suppression table is otherwise exactly the kind of
    place a real regression could go to hide, the failure shape this
    whole audit-routing task exists to fix."""

    def test_clean_table_reports_no_issues(self):
        all_defs = {"labeled_date_count": {}}
        reachable = set()
        issues = self_audit._audit_suppression_table(all_defs, reachable)
        # labeled_date_count exists and is not reachable -- no issue for it.
        self.assertEqual([i for i in issues if i["symbol"] == "labeled_date_count"], [])

    def test_suppressed_symbol_no_longer_in_source_is_flagged(self):
        with mock.patch.object(self_audit, "DEAD_CODE_SUPPRESSED", {
            "ghost_function": {
                "category": self_audit.DEAD_CODE_CATEGORY_AST_BLIND_SPOT,
                "reason": "test",
            },
        }):
            issues = self_audit._audit_suppression_table(all_defs={}, reachable=set())
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0]["symbol"], "ghost_function")
        self.assertEqual(issues[0]["problem"], "suppressed_symbol_not_found")

    def test_deliberately_dead_symbol_now_reachable_is_flagged(self):
        """The real regression this guards against: register() (or any
        DELIBERATELY_DEAD symbol) gains a real caller, meaning the
        pipeline it was kept for got wired up -- the suppression entry
        is now describing something false and must surface, not keep
        silently hiding it."""
        with mock.patch.object(self_audit, "DEAD_CODE_SUPPRESSED", {
            "register": {
                "category": self_audit.DEAD_CODE_CATEGORY_DELIBERATELY_DEAD,
                "reason": "test",
            },
        }):
            issues = self_audit._audit_suppression_table(
                all_defs={"register": {}}, reachable={"register"})
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0]["symbol"], "register")
        self.assertEqual(issues[0]["problem"], "deliberately_dead_symbol_now_reachable")

    def test_ast_blind_spot_symbol_becoming_reachable_is_not_flagged(self):
        """AST_BLIND_SPOT entries are never re-checked for reachability --
        the whole point of that category is the AST will never see the
        call that makes them live, so this isn't informative for them
        the way it is for DELIBERATELY_DEAD."""
        with mock.patch.object(self_audit, "DEAD_CODE_SUPPRESSED", {
            "momentum_strategy": {
                "category": self_audit.DEAD_CODE_CATEGORY_AST_BLIND_SPOT,
                "reason": "test",
            },
        }):
            issues = self_audit._audit_suppression_table(
                all_defs={"momentum_strategy": {}}, reachable={"momentum_strategy"})
        self.assertEqual(issues, [])

    def test_real_suppression_table_against_real_codebase_has_no_issues(self):
        """End to end against the actual repo, no mocking -- confirms the
        real, current DEAD_CODE_SUPPRESSED table is accurate right now."""
        result = self_audit.check_dead_code()
        self.assertEqual(result["detail"]["suppression_table_issues"], [])


class DeadCodeSuppressionCategoriesTest(unittest.TestCase):
    """check_dead_code()'s output against the real codebase: suppressed
    entries must be visibly split by category (a reader must be able to
    tell "confirmed live" from "deliberately dead" without opening
    source), and nothing outside the triaged get_all_signals() should
    remain flagged."""

    def test_zero_flagged_after_triage(self):
        result = self_audit.check_dead_code()
        self.assertEqual(result["detail"]["flagged"], [])
        self.assertEqual(result["status"], "PASS")

    def test_suppressed_entries_carry_distinct_categories(self):
        result = self_audit.check_dead_code()
        categories = {s["category"] for s in result["detail"]["suppressed"]}
        self.assertEqual(categories, {
            self_audit.DEAD_CODE_CATEGORY_AST_BLIND_SPOT,
            self_audit.DEAD_CODE_CATEGORY_DELIBERATELY_DEAD,
        })

    def test_registry_dispatched_strategies_are_ast_blind_spot_category(self):
        result = self_audit.check_dead_code()
        by_name = {s["name"]: s for s in result["detail"]["suppressed"]}
        for name in ("momentum_strategy", "momentum_params", "mean_rev_strategy",
                     "mean_rev_params", "relative_strategy", "relative_params",
                     "breakout_strategy", "breakout_params", "_safe_ret", "objective"):
            self.assertEqual(by_name[name]["category"],
                              self_audit.DEAD_CODE_CATEGORY_AST_BLIND_SPOT, name)

    def test_live_monitor_pipeline_symbols_are_deliberately_dead_category(self):
        result = self_audit.check_dead_code()
        by_name = {s["name"]: s for s in result["detail"]["suppressed"]}
        for name in ("labeled_date_count", "get_labelled_corpus", "register", "add_return"):
            self.assertEqual(by_name[name]["category"],
                              self_audit.DEAD_CODE_CATEGORY_DELIBERATELY_DEAD, name)


if __name__ == "__main__":
    unittest.main()
