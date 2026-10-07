#!/usr/bin/env bash
set -euo pipefail

ROOT="/srv/workspace/MoneyPrinterTurbo"
RUN_DIR="$ROOT/storage/run"
LOG="$RUN_DIR/mpt-api.log"
LOCK="$RUN_DIR/mpt-api.lock"

mkdir -p "$RUN_DIR"
cd "$ROOT"

exec 9>"$LOCK"
if ! flock -n 9; then
  exit 0
fi

while true; do
  if curl -fsS --max-time 3 http://127.0.0.1:8088/openapi.json >/dev/null 2>&1; then
    sleep 30
    continue
  fi

  echo "$(date -Is) starting MoneyPrinterTurbo API" >>"$LOG"
  PYTHONUNBUFFERED=1 .venv/bin/python main.py >>"$LOG" 2>&1 || true
  echo "$(date -Is) MoneyPrinterTurbo API exited; retrying" >>"$LOG"
  sleep 5
done
