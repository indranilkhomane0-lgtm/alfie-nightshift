#!/usr/bin/env python3
"""
Alfie Night Shift — raw OHLCV archive.

Archives the EXACT closed-bar window nightshift/stamp_prediction.py's
_context_hash() hashes for a given asset on a given night -- the same
bytes, not a re-fetch. Called from stamp()'s own call site, right where
`closed` is already in hand, immediately before _context_hash() is
computed from it. That call site is the only place in the pipeline
that both (a) has the exact window a prediction was actually made
against and (b) is already on the frozen strategy surface anyway,
because _context_hash() lives there too -- see the module docstring of
nightshift/archive_corpus.py for why an independent re-fetch from
OUTSIDE the freeze was rejected: it would not be guaranteed
byte-identical to what was actually hashed, which would make an
archive built that way misleading rather than closing the gap it
exists to close.

Format: an ever-growing reports/archive/ohlcv.csv.gz. Each archiving
call appends a NEW gzip member (gzip.open(path, "ab")) rather than
rewriting the file -- concatenated gzip members decompress back into
one continuous stream when read (gzip.open(path, "rb").read()), the
same way log rotation tooling appends to .gz files, so this stays
appendable without ever re-reading or rewriting bytes already flushed.
One CSV row per closed bar: cycle_date,asset,bar_date,source,open,high,
low,close,volume. Floats are written via Python's own repr(float(x))
-- round-trip safe (the shortest decimal string that parses back to
the exact same float64), not rounded, so a verifier who re-parses a
row and rebuilds nightshift/stamp_prediction.py's hash payload from it
reproduces the exact same context_hash, not an approximation of it.

Idempotent per (cycle_date, asset), two tiers: a process-local cache
(instant, no disk I/O) covers the common case of several candidates
for the same asset the same night all calling this; a one-time disk
scan on first use of a given key covers the cross-process case (a
cycle that was retried after an earlier failure this same calendar
night). A FAILED write is never cached as done, so the very next
candidate for that asset -- if there is one -- gets a free retry
within the same run.

The honest limit "defer and sweep" does NOT fully cover here, unlike
nightshift/archive_corpus.py's corpus-delta or nightshift/anchor_ots.py's
OTS backlog: both of those can always be re-derived or re-attempted
from something durable (corpus.db's rows persist; an OTS proof can
always be re-upgraded later). This archive's raw material -- the
`closed` DataFrame -- exists only in the running cycle process's
memory. If EVERY candidate for an asset fails to archive on a given
night (e.g. the disk stays full all night), there is no later sweep
that can recover it, because the only way to get that exact window
again would be a re-fetch, and a re-fetch is exactly the thing this
module exists to avoid relying on. A within-run retry (the next
candidate for that asset, if one exists) is the only recovery this
mechanism offers; a night where it still doesn't make it in is a
real, permanent, honestly-visible gap -- the chain entry's
ohlcv_archive_n/sha256 simply won't grow that night, and that is
disclosed, not hidden.

Like every other archiver in this pipeline: every failure mode here is
caught and logged, never raised. A write failure must never prevent
nightshift/stamp_prediction.py's stamp() from returning its prediction
normally -- the archive is a side effect of stamping, not a
precondition for it.
"""
import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

ARCHIVE_PATH = ROOT / "reports" / "archive" / "ohlcv.csv.gz"
LOG_PATH = ROOT / "nightshift" / "logs" / "archive_ohlcv.log"
IST = timezone(timedelta(hours=5, minutes=30))

CSV_COLUMNS = ("open", "high", "low", "close", "volume")

# Process-local: which (cycle_date, asset) pairs this process has
# already successfully archived. Reset every time a new cycle process
# starts (it's a fresh import) -- deliberately not persisted, since the
# disk scan in _already_on_disk() is what makes cross-process retries
# safe; this is purely a fast-path to skip that scan for the common
# within-run case of several candidates sharing an asset.
_archived_this_run: set = set()


def log(line: str) -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S IST")
    with LOG_PATH.open("a") as f:
        f.write(f"{stamp}  {line}\n")


def _already_on_disk(cycle_date: str, asset: str) -> bool:
    """Decompresses the whole existing archive to look for a row
    already covering this (cycle_date, asset) -- only called once per
    key per process (see _archived_this_run), not once per candidate.
    Missing or corrupt archive -> False, never raises: a corrupt file
    is a problem for a human to notice separately, not a reason to
    block tonight's archiving or re-raise into stamp()."""
    if not ARCHIVE_PATH.exists():
        return False
    import gzip
    needle = f"{cycle_date},{asset},"
    try:
        with gzip.open(ARCHIVE_PATH, "rt", newline="") as f:
            for line in f:
                if line.startswith(needle):
                    return True
    except Exception as exc:
        log(f"ALERT — could not read existing OHLCV archive to check for "
            f"duplicates, assuming not yet archived: {exc!r}")
        return False
    return False


def archive_closed_bars(asset: str, closed, source: str | None, cycle_date: str) -> None:
    """Append `asset`'s exact closed-bar window for `cycle_date` to the
    OHLCV archive, if not already present. `closed` is the exact
    DataFrame nightshift/stamp_prediction.py's stamp() is about to pass
    to _context_hash() -- same object, called immediately before that
    happens. Never raises."""
    key = (cycle_date, asset)
    if key in _archived_this_run:
        return
    if len(closed) == 0:
        # Nothing to archive -- and nothing to mark as done either: a
        # later candidate this same night that somehow sees a non-empty
        # window for this asset (shouldn't happen, `closed` is the same
        # object every time, but never say never) must still be able to
        # archive it rather than finding this key already "handled".
        return
    try:
        if _already_on_disk(cycle_date, asset):
            _archived_this_run.add(key)
            return

        lines = []
        for idx, row in closed.iterrows():
            bar_date = idx.isoformat() if hasattr(idx, "isoformat") else str(idx)
            values = [repr(float(row[c])) for c in CSV_COLUMNS]
            lines.append(",".join([cycle_date, asset, bar_date, source or ""] + values))
        payload = "\n".join(lines) + "\n"

        import gzip
        ARCHIVE_PATH.parent.mkdir(parents=True, exist_ok=True)
        with gzip.open(ARCHIVE_PATH, "ab") as f:
            f.write(payload.encode())

        _archived_this_run.add(key)
        log(f"archived {len(closed)} closed bar(s) for {asset} ({cycle_date})")
    except Exception as exc:
        # Deliberately NOT added to _archived_this_run on failure -- the
        # next candidate for this asset tonight, if there is one, gets
        # a free retry. See module docstring for why there is no sweep
        # beyond that.
        log(f"ALERT — could not archive OHLCV for {asset} ({cycle_date}): {exc!r}")
