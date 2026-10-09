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

Credential-expiry check, as of 2026-09-19 (chain entry 257 and this
entry): the outage this watchdog exists to catch was a dead push
credential, discovered only by hand. check_credential_expiry() makes
one nightly GET https://api.github.com/user with the same token git
push uses, and reads GitHub's own
`github-authentication-token-expiration` response header -- one call
against a 5,000/hour limit, negligible. It distinguishes three outcomes
that must not collapse into each other: the credential already being
dead (401, alerted now, distinct wording from an expiry warning), the
credential expiring within CREDENTIAL_WARN_DAYS (alerted as a warning),
and being unable to determine either (network failure, rate limit, a
response missing the expected header, an unparseable value -- logged
explicitly as "could not determine," never silently treated as "nothing
to report," since that would recreate exactly the invisible-failure
shape this file exists to avoid). The token itself is never written to
disk a second time: it's read fresh each run via `git credential fill`
(the same credential helper `git push` already uses), kept in memory
for the one API call, and discarded.

Audit-regression routing, as of 2026-10-09 (disclosed on the chain):
self_audit.py's checks were correct and chained every night, but nothing
ever read the result -- two findings sat there undetected not because
the check was wrong but because there was nowhere for a CHANGE in it to
land. check_audit_regressions() diffs tonight's AUDIT_RESULT chain entry
against the one before it and alerts on CHANGE only, never on standing
level: a check that newly fails when it passed before, a finding count
that increased on an already-failing check, a check that newly passes
again (routed at lower priority -- worth knowing, not worth the same
tone), and entries_without_ots_proof climbing (a top-level count on the
payload, not one of the per-check finding lists, which is exactly how
the 4 -> 191 OTS backlog climbed for 66 days without ever being compared
night over night). A nightly notification about a standing 17 dead-code
findings would become noise within a week and then get ignored -- the
specific failure mode this is built to not reproduce. The baseline is
the chain itself (the last two AUDIT_RESULT entries), not a separate
state file -- nothing to lose, nothing to rebuild.

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
GITHUB_API_TIMEOUT_S = 10
CREDENTIAL_WARN_DAYS = 10


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


def _load_chain_entries() -> list[dict]:
    """Every chain entry, oldest first. Used only by the audit-regression
    check below, which needs to find the last two AUDIT_RESULT entries
    regardless of what else was chained in between or after -- last_entry()
    above stays untouched as the single-entry fast path the rest of main()
    already relies on."""
    if not CHAIN_PATH.exists():
        return []
    entries = []
    with CHAIN_PATH.open() as f:
        for line in f:
            line = line.strip()
            if line:
                entries.append(json.loads(line))
    return entries


def _last_two_audit_results(entries: list[dict]) -> tuple[dict | None, dict | None]:
    """(previous, current) AUDIT_RESULT entries -- current is None if the
    chain has never carried one yet; previous is None if current is the
    first. The baseline deliberately lives in the chain itself, not a
    separate state file: the chain already carries everything needed
    (same standard the OTS confirmed-index is held to when it IS a
    separate file -- rebuildable from source of truth -- except here
    there's no need for a file at all, since the chain already is that
    source)."""
    audits = [e for e in entries if e.get("payload", {}).get("type") == "AUDIT_RESULT"]
    if not audits:
        return None, None
    if len(audits) == 1:
        return None, audits[-1]
    return audits[-2], audits[-1]


def check_audit_regressions(entries: list[dict]) -> tuple[list[str], list[str]]:
    """Diffs tonight's AUDIT_RESULT against the one before it. Returns
    (regressions, recoveries) -- both empty if nothing changed, which is
    the common, correct, silent case: self_audit.py's checks run every
    night and most nights look exactly like the night before.

    This exists to fix two findings that were detected correctly by
    self_audit.py and read by nobody, because there was nowhere for a
    CHANGE in them to land: the OTS backlog climbing 4 -> 191 over 66
    days (entries_without_ots_proof is a top-level count on the AUDIT_RESULT
    payload, not one of the per-check finding lists FINDING_KEYS counts --
    see self_audit.py -- so it never flipped ots_proof_coverage's own
    status to FAIL and nothing ever compared the number itself night over
    night), and live_monitor.register() flagged unreachable in 67 of 67
    audits since 2026-08-01 (a standing FAIL with a standing finding_count
    looks identical every single night -- only a transition is actually
    news).

    Deliberately alerts on CHANGE, never on standing level. A nightly
    notification about 17 dead symbols becomes noise within a week and
    then gets ignored -- the exact failure mode this function exists to
    avoid reproducing in a new form."""
    prev, curr = _last_two_audit_results(entries)
    if curr is None:
        return [], []
    regressions: list[str] = []
    recoveries: list[str] = []
    prev_payload = prev.get("payload", {}) if prev else {}
    curr_payload = curr.get("payload", {})
    prev_checks = prev_payload.get("checks", {})
    curr_checks = curr_payload.get("checks", {})

    for name, curr_c in curr_checks.items():
        curr_status = curr_c.get("status")
        curr_n = curr_c.get("finding_count", 0)
        prev_c = prev_checks.get(name)
        if prev_c is None:
            # New check, or first audit ever -- nothing to diff against.
            # A brand-new check that's already failing still deserves one
            # mention, since otherwise its first failure would be silent.
            if curr_status in ("FAIL", "ERROR"):
                regressions.append(
                    f"{name} is {curr_status} ({curr_n} finding(s)) -- "
                    f"no prior audit to compare against"
                )
            continue
        prev_status = prev_c.get("status")
        prev_n = prev_c.get("finding_count", 0)
        newly_failing = curr_status in ("FAIL", "ERROR") and prev_status not in ("FAIL", "ERROR")
        still_failing_but_worse = (
            curr_status in ("FAIL", "ERROR") and prev_status in ("FAIL", "ERROR")
            and curr_n > prev_n
        )
        newly_passing = curr_status == "PASS" and prev_status in ("FAIL", "ERROR")
        if newly_failing:
            regressions.append(
                f"{name} newly {curr_status} (was {prev_status}) -- {curr_n} finding(s)")
        elif still_failing_but_worse:
            regressions.append(
                f"{name} finding count increased {prev_n} -> {curr_n} (status {curr_status})")
        elif newly_passing:
            recoveries.append(f"{name} newly PASS (was {prev_status})")

    # entries_without_ots_proof: a top-level count, not inside checks{} --
    # exactly the field that climbed unnoticed in the real incident this
    # function discloses. Compared directly, independent of
    # ots_proof_coverage's own PASS/FAIL status, because the real backlog
    # grew for 66 days without that status ever flipping to FAIL (none of
    # those entries had yet crossed into orphan/missing/pending-past-grace --
    # they were each, individually, still "normal", while the total climbed).
    prev_unanchored = prev_payload.get("entries_without_ots_proof")
    curr_unanchored = curr_payload.get("entries_without_ots_proof")
    if prev_unanchored is not None and curr_unanchored is not None:
        if curr_unanchored > prev_unanchored:
            regressions.append(
                f"OTS backlog grew {prev_unanchored} -> {curr_unanchored} "
                f"entries without a proof"
            )
        elif curr_unanchored < prev_unanchored:
            recoveries.append(
                f"OTS backlog shrank {prev_unanchored} -> {curr_unanchored} "
                f"entries without a proof"
            )

    return regressions, recoveries


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


def notify_offmachine(title: str, message: str, priority: str = "urgent") -> tuple[bool, str]:
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
    a caller that wants to log the truth needs both halves.

    priority defaults to "urgent" (every pre-existing caller's behavior,
    unchanged) -- _alert_audit() below passes "default" for a recovery
    notice ("newly passes after failing"), which is worth knowing but
    not worth the same tone as a dead credential or a missed night."""
    topic = _load_env_value("NTFY_TOPIC")
    if not topic:
        return False, "NTFY_TOPIC not set in .env"
    tag = "rotating_light" if priority == "urgent" else "information_source"
    try:
        req = urllib.request.Request(
            f"https://ntfy.sh/{topic}",
            data=message.encode("utf-8"),
            headers={"Title": title, "Priority": priority, "Tags": tag},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=NTFY_TIMEOUT_S) as resp:
            status = resp.status
            return 200 <= status < 300, f"HTTP {status}"
    except urllib.error.HTTPError as exc:
        return False, f"HTTP {exc.code} {exc.reason}"
    except Exception as exc:
        return False, repr(exc)


def _get_github_token() -> str | None:
    """Read the current push credential via `git credential fill` -- the
    same credential helper (osxkeychain) `git push` itself uses. Never
    writes the token anywhere: it's returned in memory for one API call
    in check_credential_expiry() and discarded. Deliberately not read
    from .env or any file -- the token lives only in the keychain, and
    this keeps it that way rather than adding a second copy on disk.

    Never raises: git missing, no credential stored, or a malformed
    response all just mean "can't determine," which the caller must
    report explicitly rather than treat as anything else."""
    try:
        result = subprocess.run(
            ["git", "credential", "fill"],
            input="protocol=https\nhost=github.com\n\n",
            cwd=str(ROOT), capture_output=True, text=True, timeout=10,
        )
        for line in result.stdout.splitlines():
            if line.startswith("password="):
                token = line[len("password="):].strip()
                return token or None
        return None
    except Exception:
        return None


def check_credential_expiry() -> tuple[str, str]:
    """One nightly GET /user with the current push credential, checking
    for imminent or already-happened credential failure before it causes
    another silent multi-night outage like the one chain entry 256
    disclosed. Negligible cost -- one call against a 5,000/hour limit.

    Returns (status, detail):
      'dead'    -- GitHub rejected the credential now (401). This is not
                   an expiry warning, it's happening already; push will
                   fail tonight exactly as it did 2026-09-17 to 09-19.
      'warn'    -- credential valid, expires within CREDENTIAL_WARN_DAYS.
      'ok'      -- credential valid, not close to expiring.
      'unknown' -- could not determine either way (no stored credential,
                   network failure, rate limit, unexpected response, or
                   a 200 with no expiration header -- e.g. a non-expiring
                   classic PAT, or GitHub changing the response shape).
                   'unknown' must never be read as 'ok' -- a check that
                   can't run has to say so, the same lesson entry 256
                   drew about alert() failing silently.

    Never raises."""
    token = _get_github_token()
    if not token:
        return "unknown", "no credential returned by `git credential fill` (protocol=https host=github.com)"

    try:
        req = urllib.request.Request(
            "https://api.github.com/user",
            headers={"Authorization": f"token {token}"},
        )
        with urllib.request.urlopen(req, timeout=GITHUB_API_TIMEOUT_S) as resp:
            headers = resp.headers
            status = resp.status
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            return "dead", "GitHub rejected the credential (401 Bad credentials) -- push will fail until it's rotated"
        return "unknown", f"GitHub API returned HTTP {exc.code} {exc.reason}"
    except Exception as exc:
        return "unknown", f"could not reach GitHub API: {exc!r}"

    if status != 200:
        return "unknown", f"GitHub API returned unexpected HTTP {status} with no error raised"

    raw_expiry = headers.get("github-authentication-token-expiration")
    if not raw_expiry:
        return "unknown", (
            "GitHub API returned 200 but no github-authentication-token-expiration "
            "header -- token may be a non-expiring classic PAT, or GitHub changed "
            "the response shape"
        )

    try:
        expiry = datetime.strptime(raw_expiry, "%Y-%m-%d %H:%M:%S %Z").replace(tzinfo=timezone.utc)
    except ValueError as exc:
        return "unknown", f"could not parse expiration header {raw_expiry!r}: {exc!r}"

    days_remaining = (expiry - datetime.now(timezone.utc)).total_seconds() / 86400
    if days_remaining < 0:
        return "unknown", (
            f"expiration header claims the credential already expired ({raw_expiry}) "
            f"but the API call succeeded -- treat with suspicion, don't trust either signal alone"
        )
    if days_remaining <= CREDENTIAL_WARN_DAYS:
        return "warn", f"credential expires in {days_remaining:.1f} day(s), at {raw_expiry}"
    return "ok", f"credential valid, expires in {days_remaining:.1f} day(s), at {raw_expiry}"


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


def _alert_audit(message: str, priority: str = "urgent") -> None:
    """notify_offmachine() only, deliberately -- no osascript fallback.
    alert() above is specifically about a missed/failed pipeline run
    ("Night Shift MISSED" is its fixed title) and firing it for an audit
    regression would misdescribe what happened; this is its own,
    separately-logged channel under its own title, same delivery-checked
    discipline (logged either way, success or failure) as every other
    notify_offmachine() call in this file."""
    delivered, detail = notify_offmachine("Night Shift AUDIT", message, priority=priority)
    log(f"{'ntfy delivered' if delivered else 'ntfy FAILED'} — {detail}")


def _report_audit_regressions(entries: list[dict]) -> bool:
    """Runs check_audit_regressions(), logs every regression and recovery,
    and routes each through _alert_audit() -- regressions at "urgent"
    priority, recoveries at "default" (worth knowing, not worth the same
    tone). Returns True only when there were no regressions, so main()
    can fold a silent one into a non-zero exit the same way a dying
    credential already is. Deliberately does not require recoveries to be
    absent for a "clean" result -- a recovery is good news, not a
    reason to fail the run."""
    regressions, recoveries = check_audit_regressions(entries)
    for line in regressions:
        log(f"ALERT — audit regression: {line}")
        _alert_audit(f"Audit regression: {line}", priority="urgent")
    for line in recoveries:
        log(f"audit recovery (informational): {line}")
        _alert_audit(f"Audit recovered: {line}", priority="default")
    if not regressions and not recoveries:
        log("audit regression check: no change since the previous AUDIT_RESULT")
    return not regressions


def _report_credential_status() -> bool:
    """Runs check_credential_expiry(), logs the result, and alerts for
    anything other than 'ok'. Three distinct alert wordings on purpose --
    'dead' (broken now), 'warn' (broken soon), and 'unknown' (the check
    itself couldn't run) are not interchangeable, and collapsing them
    into one generic message would hide which one is true. Returns True
    only for 'ok', so main() can fold this into its exit code -- a clean
    pipeline night with a dying credential should still exit non-zero."""
    status, detail = check_credential_expiry()
    if status == "dead":
        msg = f"GitHub push credential is DEAD right now -- {detail}"
        log(f"ALERT — {msg}")
        alert(msg)
    elif status == "warn":
        msg = f"GitHub push credential expiring soon -- {detail}"
        log(f"ALERT — {msg}")
        alert(msg)
    elif status == "unknown":
        msg = f"could not determine GitHub credential expiry -- {detail}"
        log(f"ALERT — {msg}")
        alert(msg)
    else:
        log(f"credential check OK — {detail}")
    return status == "ok"


def main() -> int:
    try:
        credential_ok = _report_credential_status()
        audit_ok = _report_audit_regressions(_load_chain_entries())

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
        return 0 if (credential_ok and audit_ok) else 1
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
