#!/usr/bin/env python3
"""
Tests for the run-instant preflight and the recorded input window:

  - run_nightshift.py refuses to build a cycle less than 30s past the
    UTC daily close, or while the local date is not yet the UTC date,
    with exit code PREFLIGHT_EXIT and no side effect; on success it
    writes brief_<id>.run.json.
  - nightshift/publish_chain.py --brief chains payload["run"] from that
    sidecar and payload["input_window"] read from the OHLCV archive, so
    a stale archive shows as a mismatch.
  - nightshift/self_audit.py check 13 re-checks both from the chain.

Run directly -- venv required (importing run_nightshift.py outside this
repo's venv exits at import time, by design):

    ./venv/bin/python3 tests/test_input_window.py
"""
import gzip
import json
import logging
import os
import sys
import tempfile
import types
import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Same import-time FileHandler redirect as tests/test_run_nightshift.py:
# run_nightshift.py's logging.basicConfig() opens
# nightshift/logs/cycle_<today>.log the moment it's imported.
_import_tmp = tempfile.mkdtemp()
_real_file_handler = logging.FileHandler
logging.FileHandler = lambda filename, *a, **kw: _real_file_handler(
    str(Path(_import_tmp) / "cycle.log"), *a, **kw)
try:
    import run_nightshift as rn  # noqa: E402
finally:
    logging.FileHandler = _real_file_handler

from nightshift import publish_chain, self_audit  # noqa: E402
from nightshift.config import ASSETS  # noqa: E402


def _utc(*args):
    return datetime(*args, tzinfo=timezone.utc)


class PreflightTest(unittest.TestCase):

    def test_inside_margin_after_close_is_refused(self):
        inst = rn.run_instant(_utc(2026, 10, 29, 0, 0, 5), date(2026, 10, 29))
        self.assertEqual(inst["seconds_after_utc_close"], 5.0)
        self.assertIn("past the UTC daily close", rn.preflight_refusal(inst))

    def test_past_margin_with_dates_agreeing_runs(self):
        inst = rn.run_instant(_utc(2026, 10, 29, 0, 0, 31), date(2026, 10, 29))
        self.assertIsNone(rn.preflight_refusal(inst))

    def test_before_close_in_ist_is_refused_on_date(self):
        """23:59:30 UTC on 10-28 is 05:29:30 IST on 10-29: past the
        previous close by a day, but cycle_id would read 10-29."""
        inst = rn.run_instant(_utc(2026, 10, 28, 23, 59, 30), date(2026, 10, 29))
        self.assertGreater(inst["seconds_after_utc_close"], rn.MIN_SECONDS_AFTER_UTC_CLOSE)
        self.assertIn("differs from UTC date", rn.preflight_refusal(inst))

    def test_local_date_defaults_to_same_instant_in_system_tz(self):
        now = _utc(2026, 10, 29, 0, 1, 0)
        self.assertEqual(rn.run_instant(now)["local_date_at_start"],
                         now.astimezone().date().isoformat())

    def test_refusal_exits_before_any_side_effect(self):
        tmp = Path(tempfile.mkdtemp())
        bad = rn.run_instant(_utc(2026, 10, 29, 0, 0, 3), date(2026, 10, 29))
        built = []
        fake_cycle = types.ModuleType("nightshift.cycle")
        fake_cycle.NightShiftCycle = lambda **kw: built.append(kw)
        fake_db = types.ModuleType("nightshift.db")
        fake_db.init_db = lambda: built.append("init_db")
        with mock.patch.object(rn, "run_instant", return_value=bad), \
             mock.patch.object(rn, "FAILURE_SIDECAR", tmp / "fail.json"), \
             mock.patch.dict(sys.modules, {"nightshift.cycle": fake_cycle,
                                           "nightshift.db": fake_db}):
            with self.assertRaises(SystemExit) as cm:
                rn.cmd_full(types.SimpleNamespace(allow_synthetic_prices=False))
        self.assertEqual(cm.exception.code, rn.PREFLIGHT_EXIT)
        self.assertEqual(built, [])
        self.assertFalse((tmp / "fail.json").exists())

    def test_success_writes_run_sidecar_with_cycle_id(self):
        tmp = Path(tempfile.mkdtemp())
        brief = tmp / "brief_20261029.txt"
        good = rn.run_instant(_utc(2026, 10, 29, 0, 1, 2), date(2026, 10, 29))

        class FakeCycle:
            def __init__(self, **kw):
                self.cycle_id = 20261029

            def run(self):
                brief.write_text("brief")
                return str(brief)

        fake_cycle = types.ModuleType("nightshift.cycle")
        fake_cycle.NightShiftCycle = FakeCycle
        fake_db = types.ModuleType("nightshift.db")
        fake_db.init_db = lambda: None
        with mock.patch.object(rn, "run_instant", return_value=good), \
             mock.patch.dict(sys.modules, {"nightshift.cycle": fake_cycle,
                                           "nightshift.db": fake_db}), \
             mock.patch("builtins.print"):
            rn.cmd_full(types.SimpleNamespace(allow_synthetic_prices=False))
        rec = json.loads(brief.with_suffix(".run.json").read_text())
        self.assertEqual(rec["cycle_id"], 20261029)
        self.assertEqual(rec["seconds_after_utc_close"], 62.0)
        self.assertEqual(rec["utc_date_at_start"], "2026-10-29")
        self.assertEqual(rec["local_date_at_start"], "2026-10-29")
        for k in ("run_started_at_utc", "local_tz"):
            self.assertIn(k, rec)


def _archive(path: Path, night: str, last_bars: dict) -> None:
    """Rows shaped like reports/archive/ohlcv.csv.gz: cycle_date, asset,
    bar timestamp, source, o, h, l, c, v -- two bars per asset."""
    with gzip.open(path, "wt", newline="") as f:
        for asset, last in last_bars.items():
            if last is None:
                continue
            for bar in ("2026-01-01", last):
                f.write(f"{night},{asset},{bar}T00:00:00,binance,1,1,1,1,1\n")


class InputWindowTest(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.arc = self.tmp / "ohlcv.csv.gz"

    def test_all_assets_through_previous_day_match(self):
        _archive(self.arc, "2026-10-29", {a: "2026-10-28" for a in ASSETS})
        w = publish_chain.input_window("brief_20261029.txt", self.arc)
        self.assertTrue(w["all_match"])
        self.assertEqual(w["expected_last_closed_bar"], "2026-10-28")
        self.assertEqual(w["assets"][ASSETS[0]]["n_bars"], 2)

    def test_stale_archive_window_is_a_mismatch(self):
        """The early-fire path: archive_closed_bars() kept the short
        window, ending two days before the night."""
        bars = {a: "2026-10-28" for a in ASSETS}
        bars[ASSETS[0]] = "2026-10-27"
        _archive(self.arc, "2026-10-29", bars)
        w = publish_chain.input_window("brief_20261029.txt", self.arc)
        self.assertFalse(w["all_match"])
        self.assertFalse(w["assets"][ASSETS[0]]["matches"])
        self.assertTrue(w["assets"][ASSETS[1]]["matches"])

    def test_missing_asset_and_missing_archive(self):
        bars = {a: "2026-10-28" for a in ASSETS}
        bars[ASSETS[2]] = None
        _archive(self.arc, "2026-10-29", bars)
        w = publish_chain.input_window("brief_20261029.txt", self.arc)
        self.assertIsNone(w["assets"][ASSETS[2]]["last_closed_bar"])
        self.assertFalse(w["all_match"])
        w = publish_chain.input_window("brief_20261029.txt", self.tmp / "absent.csv.gz")
        self.assertIn("error", w)
        self.assertFalse(w["all_match"])

    def test_brief_payload_carries_run_and_input_window(self):
        brief = self.tmp / "brief_20261029.txt"
        brief.write_text("brief\n")
        run = {"run_started_at_utc": "2026-10-29T00:05:01+00:00",
               "seconds_after_utc_close": 301.0, "utc_date_at_start": "2026-10-29",
               "local_date_at_start": "2026-10-29", "local_tz": "Asia/Kolkata",
               "cycle_id": 20261029}
        brief.with_suffix(".run.json").write_text(json.dumps(run))
        _archive(self.arc, "2026-10-29", {a: "2026-10-28" for a in ASSETS})
        chain = self.tmp / "chain.jsonl"
        with mock.patch.object(publish_chain, "CHAIN_PATH", chain), \
             mock.patch.object(publish_chain, "OHLCV_ARCHIVE_PATH", self.arc), \
             mock.patch.object(publish_chain, "_stamp_at_publish_time"), \
             mock.patch.object(sys, "argv", ["publish_chain.py", "--brief", str(brief)]):
            self.assertEqual(publish_chain.main() or 0, 0)
        os.chmod(brief, 0o644)
        payload = json.loads(chain.read_text().splitlines()[-1])["payload"]
        self.assertEqual(payload["run"], run)
        self.assertTrue(payload["input_window"]["all_match"])


def _brief(name, run="__ok__", window="__ok__"):
    night = name[6:10] + "-" + name[10:12] + "-" + name[12:14]
    prev = date.fromisoformat(night).toordinal() - 1
    prev = date.fromordinal(prev).isoformat()
    p = {"type": "NIGHTLY_BRIEF", "brief_file": name}
    if run == "__ok__":
        run = {"seconds_after_utc_close": 301.0, "utc_date_at_start": night}
    if run is not None:
        p["run"] = run
    if window == "__ok__":
        window = {"assets": {a: {"last_closed_bar": prev} for a in ASSETS}}
    if window is not None:
        p["input_window"] = window
    return {"payload": p}


MARKER = {"payload": {"type": "METHODOLOGY_CHANGE",
                      "data": {self_audit.INPUT_WINDOW_MARKER_KEY: {}}}}


class Check13Test(unittest.TestCase):

    def check(self, entries):
        return self_audit.check_run_instant_and_input_window(entries)

    def test_skips_without_marker(self):
        self.assertEqual(self.check([_brief("brief_20261029.txt")])["status"], "SKIP")

    def test_briefs_before_marker_are_not_findings(self):
        old = {"payload": {"type": "NIGHTLY_BRIEF", "brief_file": "brief_20261028.txt"}}
        f = self.check([old, MARKER, _brief("brief_20261029.txt")])
        self.assertEqual(f["status"], "PASS")
        self.assertEqual(f["detail"]["briefs_checked"], 1)

    def test_each_failure_shape_is_reported(self):
        cases = {
            "margin_violations": _brief("brief_20261029.txt", run={
                "seconds_after_utc_close": 4.0, "utc_date_at_start": "2026-10-29"}),
            "utc_date_mismatches": _brief("brief_20261029.txt", run={
                "seconds_after_utc_close": 86370.0, "utc_date_at_start": "2026-10-28"}),
            "window_mismatches": _brief("brief_20261029.txt", window={
                "assets": {a: {"last_closed_bar": "2026-10-27"} for a in ASSETS}}),
            "missing_run_record": _brief("brief_20261029.txt", run=None),
            "missing_input_window": _brief("brief_20261029.txt", window=None),
        }
        for key, entry in cases.items():
            with self.subTest(key):
                f = self.check([MARKER, entry])
                self.assertEqual(f["status"], "FAIL")
                self.assertTrue(f["detail"][key])
                self.assertGreater(self_audit._finding_count(f), 0)

    def test_window_is_judged_against_brief_date_not_payload_flag(self):
        entry = _brief("brief_20261029.txt", window={
            "assets": {a: {"last_closed_bar": "2026-10-27", "matches": True} for a in ASSETS},
            "all_match": True})
        self.assertEqual(self.check([MARKER, entry])["status"], "FAIL")

    def test_margin_constant_matches_runner(self):
        self.assertEqual(self_audit.MIN_SECONDS_AFTER_UTC_CLOSE, rn.MIN_SECONDS_AFTER_UTC_CLOSE)


if __name__ == "__main__":
    unittest.main()
