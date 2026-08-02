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
upgrades prior still-pending proofs (oldest first, before tonight's own
stamp, under a bounded total time budget), so proofs become fully
verifiable over subsequent nights without a separate cron job.

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
    result = subprocess.run(
        [ots_bin, "stamp", "--timeout", str(STAMP_TIMEOUT_S), str(hash_file)],
        capture_output=True, text=True, env=ssl_cert_env(), timeout=STAMP_TIMEOUT_S + 10,
    )
    if result.returncode != 0:
        log(f"ALERT — ots stamp failed (rc={result.returncode}): {result.stderr.strip()[-300:]}")
        return False
    return True


def opportunistic_upgrade(ots_bin: str) -> None:
    """Try to complete every still-pending proof, oldest first, under a
    single bounded total time budget. Best-effort -- a proof staying
    pending is normal (Bitcoin confirmation lags) and must never be
    treated as an error. Must run before tonight's own stamp: tonight's
    proof is always the newest and can never be confirmed yet, so
    examining newest-first (or stopping after one) would permanently
    starve every older pending proof."""
    candidates = sorted(OTS_DIR.glob("*.hash.ots"), key=lambda p: p.stat().st_mtime)
    start = time.monotonic()
    for ots_file in candidates:
        if time.monotonic() - start > UPGRADE_TOTAL_BUDGET_S:
            log(f"upgrade sweep hit {UPGRADE_TOTAL_BUDGET_S}s budget -- remaining proofs will be retried next run")
            break

        try:
            info = subprocess.run(
                [ots_bin, "info", str(ots_file)],
                capture_output=True, text=True, timeout=10,
            )
            if "PendingAttestation" not in info.stdout:
                continue  # already complete, nothing to do
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
            else:
                log(f"upgrade not yet complete for {ots_file.name} (expected until Bitcoin confirms)")
        except Exception as exc:
            log(f"upgrade attempt on {ots_file.name} raised {exc!r} -- skipping")


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

        # Upgrade older pending proofs before tonight's own stamp exists --
        # otherwise tonight's (necessarily still-pending) proof would be
        # the newest and this would waste the whole run on something that
        # cannot possibly be confirmed yet.
        opportunistic_upgrade(ots_bin)

        # Stamp EVERY unanchored entry, oldest first -- not just the newest.
        # See unanchored_entries() for why the old newest-only behaviour left
        # entries permanently without a proof.
        pending = unanchored_entries()
        if not pending:
            log("all entries since anchoring start already have a proof")
            return 0

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
        return 0
    except Exception as exc:
        # Anchoring must never break the nightly run -- log and move on.
        log(f"ALERT — anchor_ots crashed with unhandled exception: {exc!r}")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
