#!/usr/bin/env python3
"""
Alfie Night Shift — dead-man's watchdog.

Runs after the last scheduled nightly-pipeline attempt. Confirms a chain
entry was actually published for tonight, and that it reached the remote.
If nothing was published at all -- the silent gap no other safeguard
catches, e.g. launchd never firing run_and_publish.sh because the laptop
was asleep the whole window -- it alerts loudly: an off-machine push
notification (see notify_offmachine()) plus a written log line. Also
alerts if what got published was itself a PIPELINE_FAILURE:
present-and-pushed is not the same as successful, and until 2026-09-16 a
cleanly-chained failure logged identically to a clean brief ("published
... pushed to origin") and fired no alert at all -- a reader had to
already suspect something was wrong to notice.

Alert delivery, as of 2026-09-19 (chain entry 256): the sole channel used
to be a local macOS notification (osascript), fired with its result never
checked. That outage's post-mortem found this Mac's notification-
permission record had no grant on file for the sender at all -- every
alert had likely been silently dropped since the watchdog was set up, not
just during that outage. notify_offmachine() replaces it as the primary,
logged channel: one stdlib-only HTTPS POST to ntfy.sh (off-machine, zero
recurring cost, no dependency on the GitHub credential this watchdog is
itself checking), with the HTTP response read back and logged either way
-- a failure here is visible in watchdog.log, not silent. osascript is
still fired afterward, best-effort, since it's free and harmless when it
works, but its result is never trusted or logged as authoritative.

No third-party packages -- stdlib (including urllib for the one HTTPS
call) plus the `osascript` and `git` binaries already on this Mac. Kept
dependency-free deliberately: launchd invokes this via a plain system
python3, not this repo's venv, and NTFY_TOPIC comes from a plain
`.env` read rather than the python-dotenv package other components use,
for the same reason.

Usage (called by launchd, see com.alfie.nightshift.watchdog.plist):
    python3 nightshift/watchdog.py
"""
from __future__ import annotations

import json
import subprocess
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CHAIN_PATH = ROOT / "reports" / "chain.jsonl"
LOG_PATH = ROOT / "nightshift" / "logs" / "watchdog.log"
ENV_PATH = ROOT / ".env"
IST = timezone(timedelta(hours=5, minutes=30))
NTFY_TIMEOUT_S = 10


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


def _load_env_value(key: str) -> str | None:
    """Minimal, dependency-free `.env` reader. watchdog.py stays stdlib-only
    (see module docstring) rather than pulling in python-dotenv like
    alfie/slack_sender.py does, because launchd runs this under the
    system python3, not this repo's venv, and a third-party import here
    would crash the nightly run silently into watchdog_launchd.log --
    the exact kind of invisible failure chain entry 256 was about."""
    if not ENV_PATH.exists():
        return None
    for line in ENV_PATH.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        if k.strip() == key:
            return v.strip().strip('"').strip("'")
    return None


def notify_offmachine(title: str, message: str) -> tuple[bool, str]:
    """POST to ntfy.sh -- the primary, logged alert channel as of chain
    entry 256. Off-machine (reaches a phone, not just this Mac), zero
    recurring cost (public ntfy.sh instance, no account needed), and
    independent of the GitHub credential this watchdog itself checks --
    this exact outage would not have taken it down too.

    Unlike the old osascript-only alert(), delivery here is checkable:
    the HTTP response is read back and returned so the caller can log
    the real outcome, rather than trusting a subprocess exit code that
    says nothing about whether the OS actually displayed anything (see
    entry 256 -- that was osascript's exact failure mode). Returns
    (success, detail), same shape as try_push(), for the same reason:
    a caller that wants to log the truth needs both halves."""
    topic = _load_env_value("NTFY_TOPIC")
    if not topic:
        return False, "NTFY_TOPIC not set in .env"
    try:
        req = urllib.request.Request(
            f"https://ntfy.sh/{topic}",
            data=message.encode("utf-8"),
            headers={"Title": title, "Priority": "urgent", "Tags": "rotating_light"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=NTFY_TIMEOUT_S) as resp:
            status = resp.status
            return 200 <= status < 300, f"HTTP {status}"
    except urllib.error.HTTPError as exc:
        return False, f"HTTP {exc.code} {exc.reason}"
    except Exception as exc:
        return False, repr(exc)


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
    delivered, detail = notify_offmachine("Night Shift MISSED", message)
    log(f"{'ntfy delivered' if delivered else 'ntfy FAILED'} — {detail}")
    # Best-effort local notification too -- free, harmless when it works,
    # but never the only channel and never logged as if it were reliable:
    # its exit code is 0 whether or not the OS actually showed anything
    # (see chain entry 256), so it isn't checkable the way ntfy is above.
    subprocess.run(
        [
            "osascript", "-e",
            f'display notification "{_sanitize_for_notification(message)}" '
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

        # A PIPELINE_FAILURE that chained and pushed cleanly used to fall
        # straight through to the OK log below -- "published, pushed to
        # origin" is true of it in exactly the same words as a real brief,
        # so it read as a clean night. Presence and pushedness say nothing
        # about whether tonight actually produced a signal; check that too.
        if kind == "PIPELINE_FAILURE":
            payload = entry.get("payload", {})
            failure_class = payload.get("failure_class", "unknown")
            exception_type = payload.get("exception_type", "unknown")
            msg = (
                f"PIPELINE_FAILURE for {today}, chained "
                f"{published.strftime('%H:%M IST')} and pushed to origin -- "
                f"no signal tonight. failure_class={failure_class} "
                f"exception_type={exception_type}."
            )
            log(f"ALERT — {msg}")
            alert(msg)
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
