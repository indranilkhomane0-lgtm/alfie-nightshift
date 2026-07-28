#!/bin/bash
cd "$(dirname "$0")" || exit 1
PY="./venv/bin/python"
LOG="nightshift/logs/publish_$(date -u +%Y%m%d).log"
DRY=0; [ "$1" = "--dry" ] && DRY=1
TODAY_BRIEF="nightshift/briefs/brief_$(date -u +%Y%m%d).txt"

echo "=== $(date -u) start (dry=$DRY) ===" >> "$LOG"

# FALLBACK GUARD -- launchd now fires this up to 3x/night as a redundancy
# measure. If tonight is already chained, don't re-run the (expensive,
# stochastic) pipeline again -- that would re-fetch fresh market data and
# could publish a second, different signal for the same night. Just make
# sure the existing entry actually reached the remote.
if grep -q "brief_$(date -u +%Y%m%d).txt" reports/chain.jsonl 2>/dev/null; then
  if git rev-parse @{u} >/dev/null 2>&1 && [ "$(git rev-parse HEAD)" != "$(git rev-parse @{u})" ]; then
    echo "already chained tonight -- retrying push only" >> "$LOG"
    if [ $DRY -eq 1 ]; then echo "DRY RUN -- would push" >> "$LOG"; exit 0; fi
    for attempt in 1 2 3; do
      if git push >> "$LOG" 2>&1; then echo "PUSHED on retry (attempt $attempt)" >> "$LOG"; exit 0; fi
      echo "retry push attempt $attempt failed -- retry in 30s" >> "$LOG"; sleep 30
    done
    echo "PUSH FAILED after 3 retry attempts" >> "$LOG"; exit 1
  fi
  echo "already chained and pushed tonight -- nothing to do" >> "$LOG"
  exit 0
fi

# B1a -- PRECONDITION GUARD: wait up to 5 min for DNS before doing anything
NET=0
for i in $(seq 1 30); do
  if nslookup github.com >/dev/null 2>&1; then NET=1; break; fi
  sleep 10
done
if [ $NET -eq 0 ]; then
  CYCLE_DATE="$(date -u +%Y%m%d)"
  if grep -qF "\"cycle_date\": \"$CYCLE_DATE\"" reports/chain.jsonl 2>/dev/null; then
    echo "NETWORK NEVER CAME UP (5 min) -- failure already chained for $CYCLE_DATE, skipping duplicate" >> "$LOG"
    exit 1
  fi
  echo "NETWORK NEVER CAME UP (5 min) -- publishing failure entry locally, will push on next successful run" >> "$LOG"
  if [ $DRY -eq 0 ]; then
    "$PY" nightshift/publish_chain.py --failed >> "$LOG" 2>&1
    "$PY" nightshift/anchor_ots.py >> "$LOG" 2>&1 || true
    git add reports/chain.jsonl reports/ots/ >> "$LOG" 2>&1
    git commit -m "Night Shift PIPELINE_FAILURE $(date -u +%Y-%m-%d) (network down)" >> "$LOG" 2>&1
  fi
  exit 1
fi
echo "network ready after ~$((i*10))s" >> "$LOG"

# run the cycle
"$PY" run_nightshift.py >> "$LOG" 2>&1
if [ $? -ne 0 ]; then
  echo "CYCLE FAILED -- publishing honest failure entry" >> "$LOG"
  if [ $DRY -eq 0 ]; then
    "$PY" nightshift/publish_chain.py --failed >> "$LOG" 2>&1
    "$PY" nightshift/anchor_ots.py >> "$LOG" 2>&1 || true
    git add reports/chain.jsonl reports/ots/ >> "$LOG" 2>&1
    git commit -m "Night Shift PIPELINE_FAILURE $(date -u +%Y-%m-%d)" >> "$LOG" 2>&1
    git push >> "$LOG" 2>&1
  fi
  exit 1
fi

# CHAIN -- append tonight's brief to the tamper-evident record
# (duplicate-guard: skip if this brief is already chained anywhere in the
# file -- label_outcomes.py below appends LABELED_OUTCOME entries after the
# brief, so the brief is not necessarily the last line)
if [ -f "$TODAY_BRIEF" ]; then
  if ! grep -q "brief_$(date -u +%Y%m%d).txt" reports/chain.jsonl 2>/dev/null; then
    "$PY" nightshift/publish_chain.py --brief "$TODAY_BRIEF" >> "$LOG" 2>&1
    "$PY" nightshift/anchor_ots.py >> "$LOG" 2>&1 || true
  else
    echo "brief already chained -- skipping duplicate" >> "$LOG"
  fi
fi

# LABEL -- grade any predictions whose settle date has arrived
"$PY" nightshift/label_outcomes.py >> "$LOG" 2>&1

git add nightshift/briefs/ reports/chain.jsonl reports/predictions.jsonl reports/ots/ >> "$LOG" 2>&1
if git diff --cached --quiet; then echo "No new brief -- nothing to publish" >> "$LOG"; exit 0; fi

if [ $DRY -eq 1 ]; then echo "DRY RUN -- would publish:" >> "$LOG"; git diff --cached --name-only >> "$LOG"; git reset -q; exit 0; fi

git commit -m "Night Shift brief $(date -u +%Y-%m-%d)" >> "$LOG" 2>&1

# B1b -- SELF-RECOVERY: retry push up to 3 times, 30s apart
PUSHED=0
for attempt in 1 2 3; do
  if git push >> "$LOG" 2>&1; then PUSHED=1; echo "PUBLISHED (attempt $attempt)" >> "$LOG"; break; fi
  echo "push attempt $attempt failed -- retry in 30s" >> "$LOG"; sleep 30
done
[ $PUSHED -eq 0 ] && { echo "PUSH FAILED after 3 attempts" >> "$LOG"; exit 1; }
echo "done" >> "$LOG"
