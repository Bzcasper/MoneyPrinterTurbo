#!/usr/bin/env bash
set -euo pipefail

ROOT="${MPT_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
RUN_DIR="${MPT_RUN_DIR:-$ROOT/storage/run}"
LOG="${MPT_LOG_FILE:-$RUN_DIR/mpt-api.log}"
LOCK="${MPT_LOCK_FILE:-$RUN_DIR/mpt-api.lock}"
PID_FILE="${MPT_PID_FILE:-$RUN_DIR/mpt-api.pid}"
CONFIG_FILE="${MPT_CONFIG_FILE:-$ROOT/config.toml}"
PORT="${MPT_LISTEN_PORT:-}"

if [[ -z "$PORT" && -f "$CONFIG_FILE" ]]; then
  PORT="$(sed -nE 's/^listen_port[[:space:]]*=[[:space:]]*([0-9]+).*/\1/p' "$CONFIG_FILE" | head -1)"
fi
PORT="${PORT:-8080}"
HEALTH_URL="${MPT_HEALTH_URL:-http://127.0.0.1:${PORT}/openapi.json}"

mkdir -p "$RUN_DIR"
cd "$ROOT"

exec 9>"$LOCK"
if ! flock -n 9; then
  exit 0
fi

echo $$ >"$PID_FILE"
cleanup() {
  rm -f "$PID_FILE"
}
trap cleanup EXIT INT TERM

while true; do
  if curl -fsS --max-time 3 "$HEALTH_URL" >/dev/null 2>&1; then
    sleep 30
    continue
  fi

  echo "$(date -Is) starting MoneyPrinterTurbo API root=$ROOT health=$HEALTH_URL" >>"$LOG"
  PYTHONUNBUFFERED=1 .venv/bin/python main.py >>"$LOG" 2>&1 || true
  echo "$(date -Is) MoneyPrinterTurbo API exited; retrying" >>"$LOG"
  sleep 5
done
