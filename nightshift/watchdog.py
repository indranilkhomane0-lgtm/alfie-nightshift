#!/usr/bin/env python3
"""
Alfie Night Shift — dead-man's watchdog.

Runs after the last scheduled nightly-pipeline attempt. Confirms a chain
entry (brief or PIPELINE_FAILURE) was actually published for tonight, and
that it reached the remote. If nothing was published at all -- the silent
gap that existing safeguards don't catch (see run_and_publish.sh's network
precondition guard, which can exit before ever calling publish_chain.py) --
it alerts loudly: macOS notification plus a written log line.

No network calls, no third-party packages -- stdlib plus the `osascript`
and `git` binaries already on this Mac.

Usage (called by launchd, see com.alfie.nightshift.watchdog.plist):
    python3 nightshift/watchdog.py
"""
import json
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CHAIN_PATH = ROOT / "reports" / "chain.jsonl"
LOG_PATH = ROOT / "nightshift" / "logs" / "watchdog.log"
IST = timezone(timedelta(hours=5, minutes=30))


def log(line: str) -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S IST")
    with LOG_PATH.open("a") as f:
        f.write(f"{stamp}  {line}\n")


def last_entry():
    if not CHAIN_PATH.exists() or CHAIN_PATH.stat().st_size == 0:
        return None
    with CHAIN_PATH.open("rb") as f:
        last_line = f.read().splitlines()[-1]
    return json.loads(last_line)


def try_push() -> tuple[bool, str]:
    """Attempt to push HEAD to its upstream. Returns (success, detail) --
    detail is git's stderr/stdout on failure, empty on success.

    This is a second, independent push attempt, made minutes after
    run_and_publish.sh's own 3-retry push loop already gave up at
    publish time -- whatever transient condition blocked that (network,
    auth token refresh, a momentarily unreachable remote) may have
    cleared by the time the watchdog runs. No retries here; one attempt,
    fast fail, so the watchdog itself doesn't hang."""
    try:
        result = subprocess.run(
            ["git", "-C", str(ROOT), "push"],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode == 0:
            return True, ""
        return False, (result.stderr or result.stdout or "").strip()
    except Exception as exc:
        return False, repr(exc)


def _sanitize_for_notification(text: str, max_len: int = 200) -> str:
    """Strip characters that would break out of the double-quoted
    AppleScript string alert() builds -- raw git stderr can contain
    either -- and cap length. The notification is a heads-up, not the
    record: watchdog.log always gets the full, unsanitized text."""
    cleaned = text.replace('"', "").replace("\\", "")
    if len(cleaned) > max_len:
        return cleaned[:max_len].rstrip() + "…"
    return cleaned


def unpushed_commits() -> bool:
    """True if HEAD is ahead of the last-known remote ref. No network hit --
    compares against git's local remote-tracking ref, not a live fetch."""
    try:
        head = subprocess.run(
            ["git", "-C", str(ROOT), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        upstream = subprocess.run(
            ["git", "-C", str(ROOT), "rev-parse", "@{u}"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        return head != upstream
    except subprocess.CalledProcessError:
        return False


def alert(message: str) -> None:
    subprocess.run(
        [
            "osascript", "-e",
            f'display notification "{message}" '
            f'with title "Night Shift MISSED" sound name "Sosumi"',
        ],
        check=False,
    )


def main() -> int:
    try:
        today = datetime.now(IST).date()
        entry = last_entry()

        if entry is None:
            msg = f"No chain entries exist at all. Expected a run for {today}."
            log(f"ALERT — {msg}")
            alert(msg)
            return 1

        published = datetime.fromisoformat(entry["published_at_utc"]).astimezone(IST)
        kind = entry.get("payload", {}).get("type", "UNKNOWN")

        if published.date() != today:
            msg = (
                f"No chain entry for {today}. Last entry was {kind}, "
                f"published {published.strftime('%Y-%m-%d %H:%M IST')}."
            )
            log(f"ALERT — {msg}")
            alert(msg)
            return 1

        if unpushed_commits():
            pushed, push_error = try_push()
            if pushed:
                log(f"SELF-HEALED — {kind} for {today} was unpushed at watchdog "
                    f"run (chained locally at {published.strftime('%H:%M IST')}); "
                    f"watchdog's push succeeded, now reaches origin.")
                return 0
            base = (
                f"{kind} for {today} was chained locally at "
                f"{published.strftime('%H:%M IST')} but never reached origin "
                f"(git push must have failed all 3 retries). Watchdog's own "
                f"push retry also failed: "
            )
            log(f"ALERT — {base}{push_error or 'no error output'}")
            alert(f"{base}{_sanitize_for_notification(push_error) or 'no error output'}")
            return 1

        log(f"OK — {kind} published {published.strftime('%H:%M IST')} for {today}, pushed to origin.")
        return 0
    except Exception as exc:
        # Anything unanticipated (corrupt chain entry, missing git binary, etc.)
        # must still land a log line -- an unhandled crash here would otherwise
        # be the one path that leaves watchdog.log silent on a bad night.
        msg = f"watchdog crashed with unhandled exception: {exc!r}"
        log(f"ALERT — {msg}")
        alert(msg)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
