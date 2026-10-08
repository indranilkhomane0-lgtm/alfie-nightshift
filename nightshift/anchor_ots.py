#!/usr/bin/env python3
"""
Alfie Night Shift — OpenTimestamps anchoring.

After each chain entry is published, stamps that entry's hash to the
Bitcoin blockchain via the free OpenTimestamps calendar servers (no cost,
no API keys). The proof is written next to the chain as
reports/ots/<entry_hash>.hash(.ots) so a stranger can verify it later with
nothing but the `ots` CLI:

    ots verify reports/ots/<entry_hash>.hash.ots

Reliability of the chain record outranks anchoring of it: every failure
mode here (ots not installed, network down, calendar timeout, malformed
chain file) is caught and logged, never raised. This script always exits
0 -- the nightly pipeline must never be blocked by it.

A same-night proof is only a *pending* calendar receipt; full Bitcoin
confirmation typically takes hours. Each run also opportunistically
upgrades prior still-pending proofs (newest-not-yet-indexed first, after
tonight's own stamping is done, under its own bounded time budget), so
proofs become fully verifiable over subsequent nights without a
separate cron job. Stamping runs first and gets first claim on the
run's time: a missing proof is worse than a pending upgrade, and
upgrades keep getting retried on later nights regardless.

The upgrade sweep tracks which hashes are already confirmed in
CONFIRMED_INDEX_PATH (nightshift/logs/ots_confirmed.index, gitignored)
so it never re-checks them -- see opportunistic_upgrade()'s own
docstring for the throughput bug this closes and why that cache is
safe to lose.

Usage (called by run_and_publish.sh after nightshift/publish_chain.py):
    python3 nightshift/anchor_ots.py
"""
import json
import shutil
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CHAIN_PATH = ROOT / "reports" / "chain.jsonl"
OTS_DIR = ROOT / "reports" / "ots"
LOG_PATH = ROOT / "nightshift" / "logs" / "anchor_ots.log"
# Local-only performance cache, gitignored: entry hashes already known
# Bitcoin-confirmed, so opportunistic_upgrade() never has to shell out to
# `ots info` for them again. NOT a source of truth for anything -- that
# remains the .hash.ots files themselves, which core/verify_chain.py and
# self_audit.py each independently inspect on every run, never reading
# this file. See opportunistic_upgrade()'s docstring for why that
# separation is what makes this cache safe to lose or corrupt.
CONFIRMED_INDEX_PATH = ROOT / "nightshift" / "logs" / "ots_confirmed.index"
IST = timezone(timedelta(hours=5, minutes=30))

STAMP_TIMEOUT_S = 15
UPGRADE_TIMEOUT_S = 15
UPGRADE_TOTAL_BUDGET_S = 30

# Entries published before this date predate anchoring and are covered by
# the hash chain alone (stated in README). Keep in sync with
# self_audit.OTS_ANCHORING_START.
ANCHORING_START = "2026-07-28"
# Bound the backlog sweep so a night that chains several entries, or a
# stretch of unreachable calendars, can never stall the nightly run.
STAMP_TOTAL_BUDGET_S = 90
# Calendars are either up or down; after this many consecutive failures,
# stop trying tonight rather than burning the budget one timeout at a time.
STAMP_MAX_CONSECUTIVE_FAILURES = 2


def find_ots() -> str | None:
    """launchd runs with a minimal PATH, so venv/bin (where pip installs
    the ots console-script) usually isn't on it. Look next to the running
    interpreter first -- that's where it actually lives -- then fall back
    to PATH for anyone running this outside the venv."""
    sibling = Path(sys.executable).parent / "ots"
    if sibling.exists():
        return str(sibling)
    return shutil.which("ots")


def log(line: str) -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S IST")
    with LOG_PATH.open("a") as f:
        f.write(f"{stamp}  {line}\n")


def last_entry_hash() -> str:
    with CHAIN_PATH.open("rb") as f:
        last_line = f.read().splitlines()[-1]
    return json.loads(last_line)["entry_hash"]


def unanchored_entries() -> list[str]:
    """Every chain entry from ANCHORING_START onward that has no .ots proof,
    in chain order (oldest first).

    Previously this script anchored only the NEWEST entry. Any night that
    chained more than one entry therefore left the earlier ones permanently
    unanchored -- the newest got a proof, everything behind it was skipped
    and never revisited. Confirmed on the live chain 2026-08-02: the
    2026-07-29 METHODOLOGY_CHANGE and both AUDIT_RESULT entries have no
    proof, and the AUDIT_RESULT case would have recurred every single night
    now that self_audit chains before the brief.

    Entries before ANCHORING_START are intentionally skipped: the README
    states they predate anchoring and are covered by the hash chain alone.
    Anchoring them now would only prove they existed as of tonight, which
    the chain already establishes more strongly.
    """
    out = []
    with CHAIN_PATH.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            ts = e.get("published_at_utc", "")
            if ts[:10] < ANCHORING_START:
                continue
            h = e.get("entry_hash")
            if h and not (OTS_DIR / f"{h}.hash.ots").exists():
                out.append(h)
    return out


def ssl_cert_env() -> dict:
    """certifi is already a transitive dep (via requests) -- point ots's
    urllib calls at its bundle. Without this, a macOS python.org install
    that never ran Install Certificates.command fails every stamp with
    SSL: CERTIFICATE_VERIFY_FAILED."""
    import os
    env = os.environ.copy()
    try:
        import certifi
        env["SSL_CERT_FILE"] = certifi.where()
    except ImportError:
        pass
    return env


def stamp(ots_bin: str, hash_file: Path) -> bool:
    try:
        result = subprocess.run(
            [ots_bin, "stamp", "--timeout", str(STAMP_TIMEOUT_S), str(hash_file)],
            capture_output=True, text=True, env=ssl_cert_env(), timeout=STAMP_TIMEOUT_S + 10,
        )
    except Exception as exc:
        # Same treatment as the upgrade path: a hung calendar server must
        # not abort the sweep -- log it, skip this entry, move on.
        log(f"ALERT — ots stamp raised {exc!r} on {hash_file.name} -- skipping")
        return False
    if result.returncode != 0:
        log(f"ALERT — ots stamp failed (rc={result.returncode}): {result.stderr.strip()[-300:]}")
        return False
    return True


def _load_confirmed_index() -> set:
    """Entry hashes already known Bitcoin-confirmed, from
    CONFIRMED_INDEX_PATH -- a pure performance cache. Missing or corrupt
    -> empty set, never raises: a confirmed Bitcoin attestation never
    becomes unconfirmed (that's the whole premise this cache relies on),
    so losing it only costs re-deriving which hashes are confirmed by
    actually looking at their .hash.ots files again -- once, until this
    file is rebuilt -- never a wrong answer anywhere, since nothing else
    in this codebase ever reads this file as a source of truth."""
    if not CONFIRMED_INDEX_PATH.exists():
        return set()
    try:
        return {l.strip() for l in CONFIRMED_INDEX_PATH.read_text().splitlines() if l.strip()}
    except Exception as exc:
        log(f"ALERT — could not read {CONFIRMED_INDEX_PATH.name}, rebuilding "
            f"it from the receipts themselves this run: {exc!r}")
        return set()


def _append_confirmed(entry_hash: str) -> None:
    """Appends one hash -- only ever called immediately after this run's
    own `ots info`/`ots upgrade` call has directly confirmed, from the
    actual .hash.ots bytes, that the attestation is really there. Never
    called speculatively, so a corrupted append (a crash mid-write) can
    at worst duplicate or drop one line -- read back as a set, a
    duplicate is harmless and a dropped one just costs one more
    redundant `ots info` call next run, not a wrong answer."""
    CONFIRMED_INDEX_PATH.parent.mkdir(parents=True, exist_ok=True)
    with CONFIRMED_INDEX_PATH.open("a") as f:
        f.write(entry_hash + "\n")


def opportunistic_upgrade(ots_bin: str) -> None:
    """Try to complete every still-pending proof, under a single bounded
    total time budget. Best-effort -- a proof staying pending is normal
    (Bitcoin confirmation lags) and must never be treated as an error.
    Runs after tonight's own stamp sweep, on its own budget, so a large
    backlog of pending upgrades can never eat into the time available
    for stamping missing proofs.

    Linear in PENDING receipts, not in chain length -- the fix for a
    second throughput bug found on the live chain 2026-10-08, same
    shape as the skip-check bug fixed 2026-10-06 but a different cause.
    That fix made the per-file skip-check itself correct (presence of a
    confirmed attestation, not absence of a pending one) -- but still
    re-ran that correct check, via a real `ots info` subprocess call,
    against literally every candidate, every night, forever. Measured
    on the live chain: 357 candidates, ~161ms/file just to skip-check,
    57.6s total -- the 30s budget died at file 181 every single run,
    11 runs in a row, 0 newly confirmed, never even reaching the 18
    genuinely-pending entries sitting at the tail. Those 18 were not
    failing: `ots upgrade` run directly on 3 of them succeeded in 4-5s
    each on the first attempt. The cause was throughput, not failure.

    CONFIRMED_INDEX_PATH lets an already-confirmed hash skip with a
    Python set lookup -- no subprocess, no `ots info` call, nanoseconds
    not milliseconds -- so the sweep's cost is now proportional to how
    many receipts are NOT YET indexed as confirmed (genuinely pending,
    or not yet seen since the index was last lost/rebuilt), not to how
    many total receipts this chain has ever produced. UPGRADE_TOTAL_BUDGET_S
    is unchanged at 30s -- per the instruction not to raise it, and
    because the fix is the shape, not the size: 30s now goes almost
    entirely to real upgrade attempts (~4-5s each under normal
    conditions) instead of being consumed by skip-checks, so it covers
    several times today's typical nightly volume on its own.

    Candidates needing a real check are sorted NEWEST-first, not
    oldest-first as before. Oldest-first made sense when every file was
    checked every run regardless (an old, long-confirmed file costs the
    same skip-check as a new one, so putting pending-likely newer files
    last just deferred them harmlessly) -- it does NOT make sense now:
    on the very first run after this fix, or any run after the index is
    lost, EVERY file is briefly "not yet indexed," and oldest-first
    would walk the same long-confirmed prefix before reaching the
    entries that actually need an upgrade attempt, reproducing the
    exact bug this fix exists to close. Newest-first means a cold index
    costs nothing but a one-time, budget-bounded indexing cost for old
    entries -- which are already confirmed and in no hurry -- while
    genuinely pending (always-newest) entries get tried first, every
    run, cold index or warm."""
    confirmed = _load_confirmed_index()
    candidates = sorted(OTS_DIR.glob("*.hash.ots"), key=lambda p: p.stat().st_mtime, reverse=True)
    to_check = [f for f in candidates if f.name[:-len(".hash.ots")] not in confirmed]

    if not to_check:
        log(f"all {len(candidates)} receipt(s) already confirmed and indexed -- nothing to check")
        return

    start = time.monotonic()
    newly_indexed = 0
    for ots_file in to_check:
        entry_hash = ots_file.name[:-len(".hash.ots")]
        if time.monotonic() - start > UPGRADE_TOTAL_BUDGET_S:
            log(f"upgrade sweep hit {UPGRADE_TOTAL_BUDGET_S}s budget -- "
                f"{len(to_check) - newly_indexed} still unindexed/pending, retried next run")
            break

        try:
            info = subprocess.run(
                [ots_bin, "info", str(ots_file)],
                capture_output=True, text=True, timeout=10,
            )
            if "BitcoinBlockHeaderAttestation" in info.stdout:
                _append_confirmed(entry_hash)
                newly_indexed += 1
                continue  # already has a confirmed attestation, now indexed
        except Exception:
            continue

        try:
            result = subprocess.run(
                [ots_bin, "upgrade", str(ots_file)],
                capture_output=True, text=True, env=ssl_cert_env(),
                timeout=UPGRADE_TIMEOUT_S + 10,
            )
            if result.returncode == 0:
                log(f"upgraded {ots_file.name} -- now Bitcoin-confirmed")
                _append_confirmed(entry_hash)
                newly_indexed += 1
            else:
                log(f"upgrade not yet complete for {ots_file.name} (expected until Bitcoin confirms)")
        except Exception as exc:
            log(f"upgrade attempt on {ots_file.name} raised {exc!r} -- skipping")
    else:
        log(f"upgrade sweep done: {newly_indexed}/{len(to_check)} newly confirmed/indexed "
            f"this run, budget not exhausted")


def main() -> int:
    try:
        ots_bin = find_ots()
        if ots_bin is None:
            log("ots CLI not installed -- skipping anchor for tonight")
            return 0

        if not CHAIN_PATH.exists() or CHAIN_PATH.stat().st_size == 0:
            log("no chain entries to anchor -- skipping")
            return 0

        OTS_DIR.mkdir(parents=True, exist_ok=True)

        # Stamp EVERY unanchored entry, oldest first -- not just the newest.
        # See unanchored_entries() for why the old newest-only behaviour left
        # entries permanently without a proof. This runs before the upgrade
        # sweep and on its own budget: missing proofs matter more than
        # pending upgrades, so stamping gets first claim on the run's time
        # instead of whatever upgrade left behind.
        pending = unanchored_entries()
        if not pending:
            log("all entries since anchoring start already have a proof")
        else:
            log(f"{len(pending)} entry(s) without a proof -- stamping oldest first")
            start = time.monotonic()
            consecutive_failures = 0
            stamped = 0
            for entry_hash in pending:
                if time.monotonic() - start > STAMP_TOTAL_BUDGET_S:
                    log(f"stamp sweep hit {STAMP_TOTAL_BUDGET_S}s budget -- "
                        f"{len(pending) - stamped} still unanchored, retried next run")
                    break
                if consecutive_failures >= STAMP_MAX_CONSECUTIVE_FAILURES:
                    log(f"{consecutive_failures} consecutive stamp failures "
                        f"(calendars likely unreachable) -- stopping for tonight, "
                        f"{len(pending) - stamped} still unanchored")
                    break

                hash_file = OTS_DIR / f"{entry_hash}.hash"
                hash_file.write_text(entry_hash)
                if stamp(ots_bin, hash_file):
                    log(f"stamped entry {entry_hash[:16]}… (pending Bitcoin confirmation)")
                    stamped += 1
                    consecutive_failures = 0
                else:
                    # Never leave a .hash without a .ots -- that combination is
                    # the crash-mid-stamp signature self_audit check 3 flags, and
                    # nothing else ever cleans it up.
                    hash_file.unlink(missing_ok=True)
                    consecutive_failures += 1

            log(f"stamp sweep done: {stamped}/{len(pending)} newly anchored")

        # Upgrade older pending proofs after tonight's stamping is done, on
        # its own separate budget -- see opportunistic_upgrade() docstring.
        opportunistic_upgrade(ots_bin)
        return 0
    except Exception as exc:
        # Anchoring must never break the nightly run -- log and move on.
        log(f"ALERT — anchor_ots crashed with unhandled exception: {exc!r}")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
