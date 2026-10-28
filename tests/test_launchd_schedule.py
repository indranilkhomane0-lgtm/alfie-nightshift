#!/usr/bin/env python3
"""
Tests for nightshift/self_audit.py check 14 (launchd_schedule): the
repo's launchd/ plists, the installed copies in ~/Library/LaunchAgents/,
and the jobs launchd has loaded must carry the same calendar intervals.
Uses temp directories and a faked `launchctl print`; never touches the
real LaunchAgents or launchd.

    python3 tests/test_launchd_schedule.py
"""
import plistlib
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from nightshift import self_audit  # noqa: E402

MAIN = "com.alfie.nightshift"
WATCH = "com.alfie.nightshift.watchdog"
FIVE = [(5, 35), (5, 50), (6, 10), (7, 0), (8, 0)]
STALE = [(5, 30), (5, 50), (6, 10)]  # the July copy


def _plist(label, slots, program="/bin/bash"):
    sci = [{"Hour": h, "Minute": m} for h, m in slots]
    return {"Label": label, "ProgramArguments": [program],
            "StartCalendarInterval": sci[0] if len(sci) == 1 else sci}


def _launchctl_print(label, slots):
    """Shape of real `launchctl print gui/<uid>/<label>` output: one event
    trigger per slot, keys in launchd's own (reversed) order."""
    triggers = "".join(
        f"\t\t{label}.{i} => {{\n\t\t\tkeepalive = 0\n\t\t\tservice = {label}\n"
        f"\t\t\tstream = com.apple.launchd.calendarinterval\n"
        f"\t\t\tmonitor = com.apple.UserEventAgent-Aqua\n"
        f"\t\t\tdescriptor = {{\n\t\t\t\t\"Minute\" => {m}\n\t\t\t\t\"Hour\" => {h}\n"
        f"\t\t\t}}\n\t\t}}\n"
        for i, (h, m) in enumerate(slots))
    return (f"gui/501/{label} = {{\n\tpath = /x/{label}.plist\n"
            f"\tevent triggers = {{\n{triggers}\t}}\n}}\n")


class LaunchdScheduleCheckTest(unittest.TestCase):

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.repo = root / "launchd"
        self.agents = root / "LaunchAgents"
        self.repo.mkdir()
        self.agents.mkdir()
        self.loaded = {}
        for patch in (mock.patch.object(self_audit, "LAUNCHD_REPO_DIR", self.repo),
                      mock.patch.object(self_audit, "LAUNCH_AGENTS_DIR", self.agents),
                      mock.patch.object(self_audit.shutil, "which", return_value="/bin/launchctl"),
                      mock.patch.object(self_audit.subprocess, "run", side_effect=self._fake_run)):
            patch.start()
            self.addCleanup(patch.stop)

    def _fake_run(self, cmd, **kw):
        label = cmd[-1].rsplit("/", 1)[-1]
        if label not in self.loaded:
            return subprocess.CompletedProcess(cmd, 113, "", "Could not find service")
        return subprocess.CompletedProcess(cmd, 0, _launchctl_print(label, self.loaded[label]), "")

    def _write(self, d, label, slots, **kw):
        (d / f"{label}.plist").write_bytes(plistlib.dumps(_plist(label, slots, **kw)))

    def _consistent(self):
        for label, slots in ((MAIN, FIVE), (WATCH, [(8, 30)])):
            self._write(self.repo, label, slots)
            self._write(self.agents, label, slots)
            self.loaded[label] = slots

    def test_all_three_agree(self):
        self._consistent()
        self.assertEqual(self_audit.check_launchd_schedule()["status"], "PASS")

    def test_stale_installed_copy_is_the_pre_fix_state(self):
        """Loaded matches the repo (bootstrapped by hand); LaunchAgents
        still holds the July copies -- what a reboot would have loaded."""
        self._consistent()
        self._write(self.agents, MAIN, STALE)
        self._write(self.agents, WATCH, [(6, 30)])
        f = self_audit.check_launchd_schedule()
        self.assertEqual(f["status"], "FAIL")
        rows = {r["label"]: r for r in f["detail"]["schedule_divergences"]}
        self.assertEqual(set(rows), {MAIN, WATCH})
        self.assertEqual(len(rows[MAIN]["repo"]), 5)
        self.assertEqual(len(rows[MAIN]["installed"]), 3)
        self.assertEqual(rows[MAIN]["loaded"], rows[MAIN]["repo"])

    def test_loaded_job_diverging_is_reported(self):
        self._consistent()
        self.loaded[MAIN] = STALE
        f = self_audit.check_launchd_schedule()
        self.assertEqual(f["status"], "FAIL")
        self.assertEqual(f["detail"]["schedule_divergences"][0]["label"], MAIN)

    def test_slot_order_does_not_matter(self):
        self._consistent()
        self.loaded[MAIN] = list(reversed(FIVE))
        self.assertEqual(self_audit.check_launchd_schedule()["status"], "PASS")

    def test_leftover_bak_is_unexpected(self):
        self._consistent()
        (self.agents / f"{MAIN}.plist.bak").write_bytes(b"old")
        f = self_audit.check_launchd_schedule()
        self.assertEqual(f["detail"]["unexpected_files"], [f"{MAIN}.plist.bak"])
        self.assertEqual(f["status"], "FAIL")

    def test_missing_install_and_unloaded_job(self):
        self._consistent()
        (self.agents / f"{WATCH}.plist").unlink()
        del self.loaded[MAIN]
        d = self_audit.check_launchd_schedule()["detail"]
        self.assertEqual(d["not_installed"], [WATCH])
        self.assertEqual(d["not_loaded"], [MAIN])

    def test_installed_copy_differing_outside_schedule(self):
        self._consistent()
        self._write(self.agents, MAIN, FIVE, program="/usr/bin/python3")
        d = self_audit.check_launchd_schedule()["detail"]
        self.assertEqual(d["installed_copy_differs"], [{"label": MAIN, "keys": ["ProgramArguments"]}])

    def test_skips_off_macos(self):
        with mock.patch.object(self_audit.shutil, "which", return_value=None):
            self.assertEqual(self_audit.check_launchd_schedule()["status"], "SKIP")

    def test_findings_are_counted(self):
        self._consistent()
        self._write(self.agents, MAIN, STALE)
        self.assertGreater(self_audit._finding_count(self_audit.check_launchd_schedule()), 0)


if __name__ == "__main__":
    unittest.main()
