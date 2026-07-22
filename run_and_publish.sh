#!/bin/bash
cd "$(dirname "$0")" || exit 1
PY="./venv/bin/python"
LOG="nightshift/logs/publish_$(date -u +%Y%m%d).log"
DRY=0; [ "$1" = "--dry" ] && DRY=1
TODAY_BRIEF="nightshift/briefs/brief_$(date -u +%Y%m%d).txt"

echo "=== $(date -u) start (dry=$DRY) ===" >> "$LOG"

# B1a -- PRECONDITION GUARD: wait up to 5 min for DNS before doing anything
NET=0
for i in $(seq 1 30); do
  if nslookup github.com >/dev/null 2>&1; then NET=1; break; fi
  sleep 10
done
if [ $NET -eq 0 ]; then echo "NETWORK NEVER CAME UP (5 min) -- aborting, retry next run" >> "$LOG"; exit 1; fi
echo "network ready after ~$((i*10))s" >> "$LOG"

# run the cycle
"$PY" run_nightshift.py >> "$LOG" 2>&1
if [ $? -ne 0 ]; then
  echo "CYCLE FAILED -- publishing honest failure entry" >> "$LOG"
  if [ $DRY -eq 0 ]; then
    "$PY" nightshift/publish_chain.py --failed >> "$LOG" 2>&1
    git add reports/chain.jsonl >> "$LOG" 2>&1
    git commit -m "Night Shift PIPELINE_FAILURE $(date -u +%Y-%m-%d)" >> "$LOG" 2>&1
    git push >> "$LOG" 2>&1
  fi
  exit 1
fi

# CHAIN -- append tonight's brief to the tamper-evident record
# (duplicate-guard: skip if this brief is already the last chained entry)
if [ -f "$TODAY_BRIEF" ]; then
  if ! tail -1 reports/chain.jsonl 2>/dev/null | grep -q "brief_$(date -u +%Y%m%d).txt"; then
    "$PY" nightshift/publish_chain.py --brief "$TODAY_BRIEF" >> "$LOG" 2>&1
  else
    echo "brief already chained -- skipping duplicate" >> "$LOG"
  fi
fi

# LABEL -- grade any predictions whose settle date has arrived
"$PY" nightshift/label_outcomes.py >> "$LOG" 2>&1

git add nightshift/briefs/ reports/chain.jsonl reports/predictions.jsonl >> "$LOG" 2>&1
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
