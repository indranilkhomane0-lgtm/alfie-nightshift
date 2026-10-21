#!/usr/bin/env python3
"""
Tests for run_nightshift.py's _innermost_named_exception() and the
_write_failure_sidecar() fix it feeds -- disclosed on the chain: four
PIPELINE_FAILURE entries (2026-10-15/18/20/21) all recorded identically
as (failure_class=upstream_data, exception_type=RuntimeError), even
though the first three were genuine DNS resolution failures
(urllib3.exceptions.NameResolutionError) and the fourth was an
unrelated mid-transfer truncation (urllib3.exceptions.ProtocolError /
requests.exceptions.ChunkedEncodingError) -- indistinguishable after
the fact, because exception_type always recorded cycle.py's own
RuntimeError wrapper, identical in shape regardless of what actually
failed underneath it.

Both real exception chains are reconstructed exactly as the real
libraries raise them (same raise/from pattern, read directly from
urllib3/requests/ccxt source), not approximated -- so these tests fail
if a future urllib3/requests/ccxt upgrade changes how deep the chain
goes or which link sets __cause__ vs __context__.

stdlib + requests/urllib3/ccxt (same as the real pipeline). Run
directly -- venv required: importing run_nightshift.py outside this
repo's venv exits the whole process at import time, by design (see
that file's own top-of-file guard):

    ./venv/bin/python3 tests/test_run_nightshift.py
"""
import json
import logging
import socket
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

try:
    import urllib3.exceptions as ue
    import requests.exceptions as re
    import ccxt
except ImportError:
    ue = None

# run_nightshift.py's own module-level logging.basicConfig() opens a
# real FileHandler against nightshift/logs/cycle_<today>.log the
# instant it's imported -- a real production write this test file is
# the first to trigger (no earlier test imported run_nightshift.py).
# Redirected to a tmp file for the duration of the import only; every
# other logging.FileHandler call anywhere else is unaffected. Not using
# TemporaryDirectory's context-manager cleanup here deliberately -- the
# FileHandler keeps this file open for the rest of the process, so the
# directory must outlive the `with` block; it's left to die with the
# test process instead (negligible, one tiny file).
_import_tmp = tempfile.mkdtemp()
_real_file_handler = logging.FileHandler
logging.FileHandler = lambda filename, *a, **kw: _real_file_handler(
    str(Path(_import_tmp) / "cycle.log"), *a, **kw)
try:
    import run_nightshift as rn  # noqa: E402
finally:
    logging.FileHandler = _real_file_handler


def _build_dns_failure() -> Exception:
    """Reproduces the real chain from nightshift/logs/publish_20261018.log:
    socket.gaierror -> urllib3.NameResolutionError -> urllib3.MaxRetryError
    -> requests.ConnectionError -> ccxt.NetworkError -> RuntimeError."""
    try:
        try:
            try:
                gai = socket.gaierror(8, "nodename nor servname provided, or not known")
                raise ue.NameResolutionError("api.binance.com", None, gai) from gai
            except ue.NameResolutionError as nre:
                raise ue.MaxRetryError(None, "/api/v3/exchangeInfo", nre) from nre
        except ue.MaxRetryError as mre:
            raise re.ConnectionError(mre)
    except re.ConnectionError as ce_:
        try:
            raise ccxt.NetworkError("binance GET https://api.binance.com/api/v3/exchangeInfo") from ce_
        except ccxt.NetworkError as ne:
            try:
                raise RuntimeError(
                    "price fetch failed for BTC/USDT, refusing to "
                    "proceed on fabricated data: binance GET ...") from ne
            except RuntimeError as rte:
                return rte


def _build_truncation_failure() -> Exception:
    """Reproduces the real chain from nightshift/logs/publish_20261021.log:
    (empty chunk line) -> urllib3.ProtocolError -> requests.ChunkedEncodingError
    -> ccxt.ExchangeError -> RuntimeError. The urllib3.response.py source
    this mirrors raises ProtocolError from inside `except ValueError:`
    when the chunk line is empty -- see tests/test_run_nightshift.py's
    own docstring note above for why this is reconstructed from source,
    not guessed."""
    try:
        try:
            try:
                int("", 16)
            except ValueError:
                raise ue.ProtocolError("Response ended prematurely") from None
        except ue.ProtocolError as pe:
            raise re.ChunkedEncodingError(pe)
    except re.ChunkedEncodingError as cee:
        try:
            raise ccxt.ExchangeError(
                "binance GET https://fapi.binance.com/fapi/v1/exchangeInfo") from cee
        except ccxt.ExchangeError as ee:
            try:
                raise RuntimeError(
                    "price fetch failed for ETH/USDT, refusing to "
                    "proceed on fabricated data: binance GET ...") from ee
            except RuntimeError as rte:
                return rte


@unittest.skipIf(ue is None, "requests/urllib3/ccxt not importable in this environment")
class InnermostNamedExceptionTest(unittest.TestCase):
    def test_dns_failure_resolves_to_name_resolution_error(self):
        leaf = rn._innermost_named_exception(_build_dns_failure())
        self.assertEqual(type(leaf).__name__, "NameResolutionError")

    def test_truncation_failure_resolves_to_protocol_error(self):
        """Stops at ProtocolError, not ChunkedEncodingError -- one level
        more specific, because ChunkedEncodingError's own __cause__ is
        None and its __context__ is exactly ProtocolError with nothing
        lost by continuing one more step. ProtocolError's own __context__
        is a bare builtins.ValueError, which is where the walk correctly
        stops (no diagnostic value gained by going further)."""
        leaf = rn._innermost_named_exception(_build_truncation_failure())
        self.assertEqual(type(leaf).__name__, "ProtocolError")

    def test_dns_and_truncation_produce_distinct_exception_types(self):
        """The actual requirement: a reader must be able to tell these
        two real, previously-indistinguishable failures apart."""
        dns_leaf = rn._innermost_named_exception(_build_dns_failure())
        trunc_leaf = rn._innermost_named_exception(_build_truncation_failure())
        self.assertNotEqual(type(dns_leaf).__name__, type(trunc_leaf).__name__)

    def test_failure_class_stays_upstream_data_for_both(self):
        """Keep failure_class as upstream_data for both -- the distinction
        belongs in exception_type, not in a second failure_class. Checked
        against the original wrapper exc, not the leaf."""
        self.assertEqual(rn._classify_failure(_build_dns_failure()), "upstream_data")
        self.assertEqual(rn._classify_failure(_build_truncation_failure()), "upstream_data")

    def test_exception_with_no_chain_returns_itself(self):
        """Worst case per the docstring: no __cause__, no __context__ ->
        returns exc unchanged, never raises."""
        try:
            raise ValueError("standalone, nothing wrapping it")
        except ValueError as exc:
            original = exc
            leaf = rn._innermost_named_exception(exc)
        self.assertIs(leaf, original)


@unittest.skipIf(ue is None, "requests/urllib3/ccxt not importable in this environment")
class WriteFailureSidecarTest(unittest.TestCase):
    """End to end through the real _write_failure_sidecar(): confirms
    the sidecar file on disk -- what publish_chain.py --failed actually
    reads -- carries the distinguishing leaf type, not the wrapper."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.sidecar_path = Path(self._tmp.name) / "last_pipeline_failure.json"
        self._patch = mock.patch.object(rn, "FAILURE_SIDECAR", self.sidecar_path)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        self._tmp.cleanup()

    def test_sidecar_records_name_resolution_error_for_dns_failure(self):
        rn._write_failure_sidecar(_build_dns_failure())
        data = json.loads(self.sidecar_path.read_text())
        self.assertEqual(data["exception_type"], "NameResolutionError")
        self.assertEqual(data["failure_class"], "upstream_data")

    def test_sidecar_records_protocol_error_for_truncation_failure(self):
        rn._write_failure_sidecar(_build_truncation_failure())
        data = json.loads(self.sidecar_path.read_text())
        self.assertEqual(data["exception_type"], "ProtocolError")
        self.assertEqual(data["failure_class"], "upstream_data")

    def test_the_two_sidecars_are_distinguishable(self):
        rn._write_failure_sidecar(_build_dns_failure())
        dns_data = json.loads(self.sidecar_path.read_text())
        rn._write_failure_sidecar(_build_truncation_failure())
        trunc_data = json.loads(self.sidecar_path.read_text())
        self.assertNotEqual(dns_data["exception_type"], trunc_data["exception_type"])
        self.assertEqual(dns_data["failure_class"], trunc_data["failure_class"])


if __name__ == "__main__":
    unittest.main()
