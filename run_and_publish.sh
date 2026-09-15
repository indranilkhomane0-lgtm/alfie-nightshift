#!/bin/bash
# B1c -- KEEP THE MAC AWAKE FOR THE WHOLE RUN.
# pmset reports `sleep 1`: this machine idle-sleeps after ONE minute. Until
# 2026-07-30 the cycle finished in ~15s and always beat that timer. On
# 2026-07-31 cycle duration jumped to ~395s (6.6 min), and from that night
# on the Mac fell asleep mid-cycle: the network guard passed at ~10s, then
# the push six minutes later hit a 75s connect timeout against a sleeping
# network stack. Nights 07-31 and 08-01 both show `network ready after ~10s`
# followed by three failed pushes -- the guard was never the problem, so
# widening its window would not have helped.
# caffeinate holds off idle/disk/system sleep for exactly as long as this
# script runs, then releases. Re-exec once, guarded by an env var so the
# nested invocation doesn't loop.
if [ -z "$ALFIE_CAFFEINATED" ] && command -v caffeinate >/dev/null 2>&1; then
  export ALFIE_CAFFEINATED=1
  exec caffeinate -i -m -s "$0" "$@"
fi
cd "$(dirname "$0")" || exit 1
PY="./venv/bin/python"
LOG="nightshift/logs/publish_$(date -u +%Y%m%d).log"
DRY=0; [ "$1" = "--dry" ] && DRY=1
TODAY_BRIEF="nightshift/briefs/brief_$(date -u +%Y%m%d).txt"

# Shared push-with-retry-then-defer, used everywhere this script pushes.
# 3 attempts, 30s apart, then gives up WITHOUT treating that as fatal --
# the commit(s) stay local and ride along with whatever the next run
# pushes. `git push` (no args, no ref) always sends the full local history
# ahead of the configured upstream, not just what this invocation added --
# so this naturally sweeps up any number of prior deferred commits too,
# not only the newest one. Never exits the script; callers decide what a
# failed push means for their own exit code.
push_or_defer() {
  for attempt in 1 2 3; do
    if git push >> "$LOG" 2>&1; then
      echo "PUSHED (attempt $attempt)" >> "$LOG"
      return 0
    fi
    echo "push attempt $attempt failed -- retry in 30s" >> "$LOG"
    sleep 30
  done
  echo "PUSH DEFERRED -- commit(s) stay local, will push on next successful run" >> "$LOG"
  return 1
}

# B1d -- SINGLE-INSTANCE LOCK.
# launchd fires this at 05:30, 05:50 and 06:10 IST. The fallback guard below
# only short-circuits once tonight's brief is ALREADY CHAINED -- so while a
# cycle is still running, a later fire sails straight past it and starts a
# second concurrent cycle writing to the same corpus.db and the same
# chain.jsonl. Cycle duration is regime-dependent and volatile (17s on
# 2026-08-02, 395s on 07-31 and 08-01), so 20-minute spacing is not a safe
# margin -- and any future WFO change that slows the cycle makes a collision
# routine rather than theoretical. macOS ships no flock(1); mkdir is atomic
# on every POSIX filesystem, so use that. A lock whose owner is gone and
# which is >2h old is treated as stale (crashed run) and reclaimed.
LOCKDIR="nightshift/.run.lock"
if ! mkdir "$LOCKDIR" 2>/dev/null; then
  LOCKPID=$(cat "$LOCKDIR/pid" 2>/dev/null)
  if [ -n "$LOCKPID" ] && kill -0 "$LOCKPID" 2>/dev/null; then
    echo "=== $(date -u) SKIPPED -- run pid $LOCKPID still active ===" >> "$LOG"
    exit 0
  fi
  if [ -z "$(find "$LOCKDIR" -maxdepth 0 -mmin +120 2>/dev/null)" ]; then
    echo "=== $(date -u) SKIPPED -- lock held (pid ${LOCKPID:-unknown} gone, <2h old) ===" >> "$LOG"
    exit 0
  fi
  echo "=== $(date -u) stale lock >2h (pid ${LOCKPID:-unknown} gone) -- reclaiming ===" >> "$LOG"
  rm -rf "$LOCKDIR" && mkdir "$LOCKDIR" || { echo "could not reclaim lock" >> "$LOG"; exit 1; }
fi
echo $$ > "$LOCKDIR/pid"
trap 'rm -rf "$LOCKDIR"' EXIT INT TERM

echo "=== $(date -u) start (dry=$DRY) ===" >> "$LOG"

# AUDIT -- runs FIRST, before every guard below, because the guards exit
# early on exactly the nights the audit most needs to see: an already-
# chained night, a network failure, a failed cycle. Placing it after them
# (as originally wired) meant it only ran on clean nights -- the inverse
# of what calendar-gap coverage is for. It audits the settled record and
# the current source, neither of which depends on tonight's run, so
# running before the cycle costs nothing. Idempotent per UTC day and
# wrapped in its own try/except -- never blocks. It commits and pushes
# its own output, since the paths below may exit before ever reaching the
# commit machinery at the end of this script.
if [ $DRY -eq 0 ]; then
  "$PY" nightshift/self_audit.py >> "$LOG" 2>&1 || true
  # Anchor the AUDIT_RESULT immediately. anchor_ots.py only stamps the
  # NEWEST chain entry, so if we wait until after tonight's brief is
  # chained, the audit entry is no longer newest and never gets a proof --
  # one permanently orphaned entry per night, forever. Both AUDIT_RESULTs
  # written before this line was added (2026-08-01, 2026-08-02) have no
  # .hash and no .ots for exactly that reason.
  "$PY" nightshift/anchor_ots.py >> "$LOG" 2>&1 || true
  if ! git diff --quiet reports/chain.jsonl 2>/dev/null || [ -n "$(git status --porcelain reports/audit/ reports/ots/ 2>/dev/null)" ]; then
    git add reports/chain.jsonl reports/audit/ reports/ots/ >> "$LOG" 2>&1
    git commit -m "Night Shift self-audit $(date -u +%Y-%m-%d)" >> "$LOG" 2>&1
    git push >> "$LOG" 2>&1 || echo "audit push failed -- will ride along with a later push" >> "$LOG"
  fi
else
  echo "DRY RUN -- would run self_audit.py" >> "$LOG"
fi

# FALLBACK GUARD -- launchd now fires this up to 3x/night as a redundancy
# measure. If tonight is already chained, don't re-run the (expensive,
# stochastic) pipeline again -- that would re-fetch fresh market data and
# could publish a second, different signal for the same night. Just make
# sure the existing entry actually reached the remote.
if grep -q "brief_$(date -u +%Y%m%d).txt" reports/chain.jsonl 2>/dev/null; then
  if git rev-parse @{u} >/dev/null 2>&1 && [ "$(git rev-parse HEAD)" != "$(git rev-parse @{u})" ]; then
    echo "already chained tonight -- retrying push only" >> "$LOG"
    if [ $DRY -eq 1 ]; then echo "DRY RUN -- would push" >> "$LOG"; exit 0; fi
    push_or_defer
    exit $?
  fi
  echo "already chained and pushed tonight -- nothing to do" >> "$LOG"
  exit 0
fi

# B1a -- NETWORK PRECONDITION GUARD -- REMOVED 2026-09-16.
# This used to curl github.com for up to 5 min before doing anything, and
# if it never came up, published a PIPELINE_FAILURE and exited WITHOUT
# ever attempting the cycle. That conflated two different questions --
# "can we compute tonight's signal" and "can we publish it" -- into one:
# 11 of the first 12 PIPELINE_FAILURE entries came from this branch,
# meaning ~11 nights recorded no signal for a reason that had nothing to
# do with whether a signal could have been produced. The root-cause
# finding above (Mac wakes at 5:25 IST, Wi-Fi hasn't associated by 5:30)
# is still accurate and still the likely cause of most of those -- but
# testing github.com reachability was never actually a precondition for
# running the cycle (which needs Binance, not GitHub) or for computing a
# signal; it only mattered for the push at the very end.
#
# The cycle now always runs. If the network genuinely isn't up yet,
# nightshift/cycle.py's load_price_data() fails the exchange fetch on its
# own and raises -- caught below as a normal cycle failure, now correctly
# attributed (see nightshift/run_nightshift.py's failure classification:
# failure_class "upstream_data") instead of masked behind a generic
# pre-emptive network guard. If Wi-Fi associates a few seconds into the
# run, the cycle simply succeeds. Either way, this launchd fire actually
# tries -- and if it doesn't work out, the 05:50 and 06:10 retries get a
# real second and third attempt at producing a signal, not just a
# push-retry on an already-declared failure.
CYCLE_DATE="$(date -u +%Y%m%d)"

# run the cycle
"$PY" run_nightshift.py >> "$LOG" 2>&1
if [ $? -ne 0 ]; then
  echo "CYCLE FAILED -- publishing honest failure entry" >> "$LOG"
  if [ $DRY -eq 0 ]; then
    if grep -qF "\"cycle_date\": \"$CYCLE_DATE\"" reports/chain.jsonl 2>/dev/null; then
      echo "failure already chained for $CYCLE_DATE -- skipping duplicate" >> "$LOG"
    else
      "$PY" nightshift/publish_chain.py --failed >> "$LOG" 2>&1
      "$PY" nightshift/anchor_ots.py >> "$LOG" 2>&1 || true
      git add reports/chain.jsonl reports/ots/ >> "$LOG" 2>&1
      git commit -m "Night Shift PIPELINE_FAILURE $(date -u +%Y-%m-%d)" >> "$LOG" 2>&1
      push_or_defer
    fi
  fi
  exit 1
fi

# CHAIN -- append tonight's brief to the tamper-evident record
# (duplicate-guard: skip if this brief is already chained anywhere in the
# file -- label_outcomes.py below appends LABELED_OUTCOME entries after the
# brief, so the brief is not necessarily the last line)
if [ -f "$TODAY_BRIEF" ]; then
  if [ $DRY -eq 1 ]; then
    echo "DRY RUN -- would chain brief + anchor (skipped)" >> "$LOG"
  elif ! grep -q "brief_$(date -u +%Y%m%d).txt" reports/chain.jsonl 2>/dev/null; then
    "$PY" nightshift/publish_chain.py --brief "$TODAY_BRIEF" >> "$LOG" 2>&1
    "$PY" nightshift/anchor_ots.py >> "$LOG" 2>&1 || true
  else
    echo "brief already chained -- skipping duplicate" >> "$LOG"
  fi
fi

# LABEL -- grade any predictions whose settle date has arrived
# NOTE (2026-08-02): --dry previously gated only the git commit/push, so a
# "dry" run still ran the cycle, chained a NIGHTLY_BRIEF, anchored it, and
# wrote LABELED_OUTCOME entries -- i.e. it mutated the permanent record
# under a flag whose name promises it won't. Found by running --dry during
# a debugging session and watching the chain grow by two entries. Both the
# CHAIN block above and the LABEL call below are now gated.
if [ $DRY -eq 1 ]; then
  echo "DRY RUN -- would grade settled predictions (skipped)" >> "$LOG"
else
  "$PY" nightshift/label_outcomes.py >> "$LOG" 2>&1
fi

git add nightshift/briefs/ reports/chain.jsonl reports/predictions.jsonl reports/ots/ reports/audit/ >> "$LOG" 2>&1
if git diff --cached --quiet; then echo "No new brief -- nothing to publish" >> "$LOG"; exit 0; fi

if [ $DRY -eq 1 ]; then echo "DRY RUN -- would publish:" >> "$LOG"; git diff --cached --name-only >> "$LOG"; git reset -q; exit 0; fi

git commit -m "Night Shift brief $(date -u +%Y-%m-%d)" >> "$LOG" 2>&1

# B1b -- SELF-RECOVERY: push with retry, deferring to a later run on
# repeated failure rather than treating tonight's success as incomplete.
push_or_defer
exit $?
