#!/usr/bin/env bash
# Ежедневный прогон. На хосте: крон. В Docker лучше:
#   docker compose --profile daily run --rm daily
set -u
ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT" || exit 1

DATE=$(date +%F)
mkdir -p runs
LOG="runs/cron-${DATE}.log"
PY="${PYTHON:-python}"
if [ -x .venv/bin/python ]; then PY=.venv/bin/python; fi

echo "[$(date '+%F %T')] старт дневного прогона" | tee -a runs/cron.log
"$PY" -u run_daily.py run >>"$LOG" 2>&1
CODE=$?
echo "[$(date '+%F %T')] финиш, код $CODE (подробности: $LOG)" | tee -a runs/cron.log
find runs -maxdepth 1 -name 'cron-*.log' -mtime +14 -delete 2>/dev/null
exit $CODE
