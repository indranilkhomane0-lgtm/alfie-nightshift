#!/usr/bin/env python3
"""
Tests for nightshift/watchdog.py's audit-regression routing
(check_audit_regressions/_report_audit_regressions) -- the fix for two
findings self_audit.py detected correctly and nobody read: the OTS
backlog climbing 4 -> 191 over 66 days, and live_monitor.register()
flagged unreachable in every audit since 2026-08-01. Both were real,
both were chained, neither had anywhere for a CHANGE to land.

check_audit_regressions() is a pure function over two AUDIT_RESULT
payloads (no chain file, no network) -- these tests build synthetic
chain-entry lists directly, same style as feeding _DefCollector real
ASTs in self_audit.py's own tests. _report_audit_regressions() is
tested separately with notify_offmachine() mocked, confirming routing
(which priority, which message) without a real network call.

stdlib only. Run directly:

    python3 tests/test_watchdog.py
"""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from nightshift import watchdog  # noqa: E402


def _audit_entry(checks, entries_without_ots_proof=0, status="PASS"):
    """One synthetic AUDIT_RESULT chain entry carrying only the fields
    check_audit_regressions() reads -- same shape self_audit.py's main()
    actually chains (see its `summary` dict)."""
    return {
        "payload": {
            "type": "AUDIT_RESULT",
            "status": status,
            "checks": checks,
            "entries_without_ots_proof": entries_without_ots_proof,
        }
    }


class CheckAuditRegressionsTest(unittest.TestCase):
    def test_no_prior_audit_at_all_reports_nothing(self):
        regressions, recoveries = watchdog.check_audit_regressions([])
        self.assertEqual(regressions, [])
        self.assertEqual(recoveries, [])

    def test_first_ever_audit_with_a_failing_check_is_reported_once(self):
        entries = [_audit_entry({"dead_code_reachability": {"status": "FAIL", "finding_count": 17}},
                                 status="FAIL")]
        regressions, recoveries = watchdog.check_audit_regressions(entries)
        self.assertEqual(len(regressions), 1)
        self.assertIn("dead_code_reachability", regressions[0])
        self.assertIn("no prior audit", regressions[0])
        self.assertEqual(recoveries, [])

    def test_standing_fail_same_count_is_silent(self):
        """The exact failure mode being fixed: a check that was FAIL
        yesterday and is FAIL again tonight, same finding_count, must
        produce NO alert -- that's the standing-level noise the whole
        feature exists to suppress."""
        prev = _audit_entry({"dead_code_reachability": {"status": "FAIL", "finding_count": 17}},
                             status="FAIL")
        curr = _audit_entry({"dead_code_reachability": {"status": "FAIL", "finding_count": 17}},
                             status="FAIL")
        regressions, recoveries = watchdog.check_audit_regressions([prev, curr])
        self.assertEqual(regressions, [])
        self.assertEqual(recoveries, [])

    def test_standing_pass_is_silent(self):
        prev = _audit_entry({"calendar_coverage": {"status": "PASS", "finding_count": 0}})
        curr = _audit_entry({"calendar_coverage": {"status": "PASS", "finding_count": 0}})
        regressions, recoveries = watchdog.check_audit_regressions([prev, curr])
        self.assertEqual(regressions, [])
        self.assertEqual(recoveries, [])

    def test_newly_failing_check_is_a_regression(self):
        prev = _audit_entry({"gate_registry_match": {"status": "PASS", "finding_count": 0}})
        curr = _audit_entry({"gate_registry_match": {"status": "FAIL", "finding_count": 1}},
                             status="FAIL")
        regressions, recoveries = watchdog.check_audit_regressions([prev, curr])
        self.assertEqual(len(regressions), 1)
        self.assertIn("gate_registry_match", regressions[0])
        self.assertIn("newly FAIL", regressions[0])
        self.assertEqual(recoveries, [])

    def test_finding_count_increase_on_already_failing_check_is_a_regression(self):
        """Not newly failing -- still FAIL both nights -- but worse.
        This is the second of the three categories: a finding count that
        increased."""
        prev = _audit_entry({"dead_code_reachability": {"status": "FAIL", "finding_count": 17}},
                             status="FAIL")
        curr = _audit_entry({"dead_code_reachability": {"status": "FAIL", "finding_count": 19}},
                             status="FAIL")
        regressions, recoveries = watchdog.check_audit_regressions([prev, curr])
        self.assertEqual(len(regressions), 1)
        self.assertIn("17 -> 19", regressions[0])
        self.assertEqual(recoveries, [])

    def test_finding_count_decrease_on_still_failing_check_is_not_a_recovery(self):
        """Improved but not fixed -- status stayed FAIL. Worth seeing in
        the audit report itself, but not alert-worthy on its own; only a
        transition to PASS is a recovery."""
        prev = _audit_entry({"dead_code_reachability": {"status": "FAIL", "finding_count": 19}},
                             status="FAIL")
        curr = _audit_entry({"dead_code_reachability": {"status": "FAIL", "finding_count": 17}},
                             status="FAIL")
        regressions, recoveries = watchdog.check_audit_regressions([prev, curr])
        self.assertEqual(regressions, [])
        self.assertEqual(recoveries, [])

    def test_newly_passing_check_is_a_recovery_not_a_regression(self):
        prev = _audit_entry({"gate_registry_match": {"status": "FAIL", "finding_count": 1}},
                             status="FAIL")
        curr = _audit_entry({"gate_registry_match": {"status": "PASS", "finding_count": 0}})
        regressions, recoveries = watchdog.check_audit_regressions([prev, curr])
        self.assertEqual(regressions, [])
        self.assertEqual(len(recoveries), 1)
        self.assertIn("gate_registry_match", recoveries[0])
        self.assertIn("newly PASS", recoveries[0])

    def test_ots_backlog_growth_is_a_regression_even_though_status_never_fails(self):
        """The real disclosed incident, reproduced directly: 4 -> 191
        over many nights, with ots_proof_coverage's own status staying
        PASS the entire time (none of those entries individually crossed
        into orphan/missing/pending-past-grace). Must still alert."""
        prev = _audit_entry({"ots_proof_coverage": {"status": "PASS", "finding_count": 0}},
                             entries_without_ots_proof=4)
        curr = _audit_entry({"ots_proof_coverage": {"status": "PASS", "finding_count": 0}},
                             entries_without_ots_proof=191)
        regressions, recoveries = watchdog.check_audit_regressions([prev, curr])
        self.assertEqual(len(regressions), 1)
        self.assertIn("4 -> 191", regressions[0])
        self.assertEqual(recoveries, [])

    def test_ots_backlog_shrinking_is_a_recovery(self):
        prev = _audit_entry({}, entries_without_ots_proof=191)
        curr = _audit_entry({}, entries_without_ots_proof=4)
        regressions, recoveries = watchdog.check_audit_regressions([prev, curr])
        self.assertEqual(regressions, [])
        self.assertEqual(len(recoveries), 1)
        self.assertIn("191 -> 4", recoveries[0])

    def test_missing_entries_without_ots_proof_field_is_skipped_not_crashed(self):
        """Entries published before this field existed carry no such key --
        same 'predates this field' convention used elsewhere on the
        chain. Must compare as 'nothing to compare', not crash or
        fabricate a 0."""
        prev = {"payload": {"type": "AUDIT_RESULT", "checks": {}}}
        curr = {"payload": {"type": "AUDIT_RESULT", "checks": {}}}
        regressions, recoveries = watchdog.check_audit_regressions([prev, curr])
        self.assertEqual(regressions, [])
        self.assertEqual(recoveries, [])

    def test_only_compares_the_last_two_audit_results_ignoring_other_entry_types(self):
        older_fail = _audit_entry({"calendar_coverage": {"status": "FAIL", "finding_count": 1}},
                                   status="FAIL")
        brief = {"payload": {"type": "NIGHTLY_BRIEF"}}
        recent_pass = _audit_entry({"calendar_coverage": {"status": "PASS", "finding_count": 0}})
        regressions, recoveries = watchdog.check_audit_regressions(
            [older_fail, brief, recent_pass])
        self.assertEqual(regressions, [])
        self.assertEqual(len(recoveries), 1)
        self.assertIn("calendar_coverage", recoveries[0])

    def test_new_check_with_no_prior_baseline_but_passing_is_silent(self):
        prev = _audit_entry({"calendar_coverage": {"status": "PASS", "finding_count": 0}})
        curr = _audit_entry({
            "calendar_coverage": {"status": "PASS", "finding_count": 0},
            "brand_new_check": {"status": "PASS", "finding_count": 0},
        })
        regressions, recoveries = watchdog.check_audit_regressions([prev, curr])
        self.assertEqual(regressions, [])
        self.assertEqual(recoveries, [])


class ReportAuditRegressionsRoutingTest(unittest.TestCase):
    """_report_audit_regressions() routes through notify_offmachine() --
    checkable, logged delivery, same discipline as every other alert in
    this file -- with regressions at urgent priority and recoveries at
    default (lower urgency, per spec: 'worth knowing, lower urgency')."""

    def setUp(self):
        # Isolate the real nightshift/logs/watchdog.log -- log() writes
        # there unconditionally, and without this, every run of this
        # test class pollutes the production log with synthetic
        # "gate_registry_match newly FAIL/PASS" lines indistinguishable
        # from a real alert to anyone reading it later. Caught exactly
        # that way: real entries never showed gate_registry_match
        # failing, but watchdog.log had lines claiming it did.
        self._tmp_dir = tempfile.TemporaryDirectory()
        self._log_patch = mock.patch.object(
            watchdog, "LOG_PATH", Path(self._tmp_dir.name) / "watchdog.log")
        self._log_patch.start()
        self._patch = mock.patch.object(watchdog, "notify_offmachine",
                                         return_value=(True, "HTTP 200"))
        self.mock_notify = self._patch.start()

    def tearDown(self):
        self._patch.stop()
        self._log_patch.stop()
        self._tmp_dir.cleanup()

    def test_regression_alerted_at_urgent_priority(self):
        prev = _audit_entry({"gate_registry_match": {"status": "PASS", "finding_count": 0}})
        curr = _audit_entry({"gate_registry_match": {"status": "FAIL", "finding_count": 1}},
                             status="FAIL")
        ok = watchdog._report_audit_regressions([prev, curr])
        self.assertFalse(ok)
        self.mock_notify.assert_called_once()
        args, kwargs = self.mock_notify.call_args
        self.assertEqual(args[0], "Night Shift AUDIT")
        self.assertIn("gate_registry_match", args[1])
        self.assertEqual(kwargs.get("priority"), "urgent")

    def test_recovery_alerted_at_default_priority(self):
        prev = _audit_entry({"gate_registry_match": {"status": "FAIL", "finding_count": 1}},
                             status="FAIL")
        curr = _audit_entry({"gate_registry_match": {"status": "PASS", "finding_count": 0}})
        ok = watchdog._report_audit_regressions([prev, curr])
        self.assertTrue(ok, "a recovery alone must not count as a regression")
        self.mock_notify.assert_called_once()
        args, kwargs = self.mock_notify.call_args
        self.assertEqual(kwargs.get("priority"), "default")

    def test_no_change_sends_no_notification(self):
        prev = _audit_entry({"calendar_coverage": {"status": "PASS", "finding_count": 0}})
        curr = _audit_entry({"calendar_coverage": {"status": "PASS", "finding_count": 0}})
        ok = watchdog._report_audit_regressions([prev, curr])
        self.assertTrue(ok)
        self.mock_notify.assert_not_called()


class NotifyOffmachinePriorityTest(unittest.TestCase):
    """notify_offmachine()'s new priority parameter must default to
    "urgent" -- every pre-existing call site (credential dead/warn,
    missed pipeline) relies on that default and passes no priority."""

    def test_default_priority_is_urgent(self):
        with mock.patch.object(watchdog, "_load_env_value", return_value="fake-topic"), \
             mock.patch("urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.return_value.__enter__.return_value.status = 200
            watchdog.notify_offmachine("Title", "message")
        req = mock_urlopen.call_args[0][0]
        self.assertEqual(req.headers.get("Priority"), "urgent")

    def test_explicit_default_priority_changes_tag_too(self):
        with mock.patch.object(watchdog, "_load_env_value", return_value="fake-topic"), \
             mock.patch("urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.return_value.__enter__.return_value.status = 200
            watchdog.notify_offmachine("Title", "message", priority="default")
        req = mock_urlopen.call_args[0][0]
        self.assertEqual(req.headers.get("Priority"), "default")
        self.assertEqual(req.headers.get("Tags"), "information_source")


def _graduation_entry(first_training=True, **data_overrides):
    data = {
        "cycle_id": 42, "date": "2026-10-20", "first_training": first_training,
        "n_train": 30, "cv_auc": 0.71, "cv_r2": 0.22,
        "gate_counter_name": "labeled_prediction_date_count", "gate_counter_value": 30,
    }
    data.update(data_overrides)
    return {
        "entry_hash": "deadbeef" * 8,
        "payload": {"type": "METHODOLOGY_CHANGE", "data": data},
    }


class FindGraduationEntryTest(unittest.TestCase):
    def test_no_graduation_entry_returns_none(self):
        self.assertIsNone(watchdog.find_graduation_entry([]))
        self.assertIsNone(watchdog.find_graduation_entry(
            [{"payload": {"type": "NIGHTLY_BRIEF"}}]))

    def test_unrelated_methodology_change_is_not_mistaken_for_graduation(self):
        """A METHODOLOGY_CHANGE entry without first_training=True (e.g.
        the dead-code triage disclosure, or any other methodology note)
        must not be mistaken for graduation."""
        other = {"payload": {"type": "METHODOLOGY_CHANGE", "data": {"some_other_field": 1}}}
        self.assertIsNone(watchdog.find_graduation_entry([other]))

    def test_graduation_entry_is_found(self):
        grad = _graduation_entry()
        found = watchdog.find_graduation_entry([{"payload": {"type": "NIGHTLY_BRIEF"}}, grad])
        self.assertIs(found, grad)


class ReportGraduationTest(unittest.TestCase):
    """_report_graduation()'s durability contract: fires once, off-machine,
    only after a confirmed delivery -- a failed delivery must retry on
    the next run rather than silently marking itself done."""

    def setUp(self):
        self._tmp_dir = tempfile.TemporaryDirectory()
        self._marker_path = Path(self._tmp_dir.name) / ".graduation_notified"
        self._marker_patch = mock.patch.object(
            watchdog, "GRADUATION_NOTIFIED_PATH", self._marker_path)
        self._marker_patch.start()
        self._log_patch = mock.patch.object(
            watchdog, "LOG_PATH", Path(self._tmp_dir.name) / "watchdog.log")
        self._log_patch.start()

    def tearDown(self):
        self._log_patch.stop()
        self._marker_patch.stop()
        self._tmp_dir.cleanup()

    def test_no_graduation_yet_sends_nothing_and_writes_no_marker(self):
        with mock.patch.object(watchdog, "notify_offmachine") as mock_notify:
            watchdog._report_graduation([])
        mock_notify.assert_not_called()
        self.assertFalse(self._marker_path.exists())

    def test_graduation_found_and_delivered_sends_once_and_writes_marker(self):
        grad = _graduation_entry()
        with mock.patch.object(watchdog, "notify_offmachine",
                                return_value=(True, "HTTP 200")) as mock_notify:
            watchdog._report_graduation([grad])
        mock_notify.assert_called_once()
        args, kwargs = mock_notify.call_args
        self.assertEqual(args[0], "Night Shift GRADUATED")
        self.assertIn("n_train=30", args[1])
        self.assertEqual(kwargs.get("priority"), "urgent")
        self.assertTrue(self._marker_path.exists())

    def test_already_notified_never_sends_again(self):
        self._marker_path.parent.mkdir(parents=True, exist_ok=True)
        self._marker_path.write_text("deadbeef" * 8 + "\n")
        grad = _graduation_entry()
        with mock.patch.object(watchdog, "notify_offmachine") as mock_notify:
            watchdog._report_graduation([grad])
        mock_notify.assert_not_called()

    def test_failed_delivery_does_not_write_marker_so_it_retries(self):
        grad = _graduation_entry()
        with mock.patch.object(watchdog, "notify_offmachine",
                                return_value=(False, "NTFY_TOPIC not set in .env")):
            watchdog._report_graduation([grad])
        self.assertFalse(self._marker_path.exists(),
                          "a failed delivery must not be marked as sent -- next run must retry")

    def test_retries_after_a_failed_delivery_once_ntfy_recovers(self):
        grad = _graduation_entry()
        with mock.patch.object(watchdog, "notify_offmachine", return_value=(False, "down")):
            watchdog._report_graduation([grad])
        self.assertFalse(self._marker_path.exists())
        with mock.patch.object(watchdog, "notify_offmachine",
                                return_value=(True, "HTTP 200")) as mock_notify:
            watchdog._report_graduation([grad])
        mock_notify.assert_called_once()
        self.assertTrue(self._marker_path.exists())


if __name__ == "__main__":
    unittest.main()
