#!/usr/bin/env bash
set -Eeuo pipefail

BASE_DIR="${HUOHUA_BASE_DIR:-/opt/huohua/fire}"
PYTHON_BIN="${HUOHUA_PYTHON_BIN:-/opt/huohua/venv/bin/python}"
LOCK_FILE="${HUOHUA_LOCK_FILE:-/var/lock/huohua-production.lock}"
STATUS_FILE="${HUOHUA_STATUS_FILE:-/var/lib/huohua/last-run.status}"
LOG_FILE="${HUOHUA_RUN_LOG:-/var/log/huohua-run.log}"
MAX_ATTEMPTS="${HUOHUA_MAX_ATTEMPTS:-3}"

mkdir -p "$(dirname "$LOCK_FILE")" "$(dirname "$STATUS_FILE")" "$(dirname "$LOG_FILE")"
exec 9>"$LOCK_FILE"
if ! flock -n 9; then
  echo "$(date -Is) another production run is already active" >>"$LOG_FILE"
  exit 0
fi

umask 077
cd "$BASE_DIR"
run_id="$(date -u +%Y%m%dT%H%M%SZ)-$$"
printf 'running %s %s\n' "$run_id" "$(date -Is)" >"$STATUS_FILE"

attempt=1
while (( attempt <= MAX_ATTEMPTS )); do
  echo "$(date -Is) run=${run_id} attempt=${attempt}/${MAX_ATTEMPTS}" >>"$LOG_FILE"
  if "$PYTHON_BIN" "$BASE_DIR/deploy/production_runner.py" >>"$LOG_FILE" 2>&1; then
    printf 'success %s %s\n' "$run_id" "$(date -Is)" >"$STATUS_FILE"
    echo "$(date -Is) run=${run_id} success" >>"$LOG_FILE"
    exit 0
  fi
  exit_code=$?
  echo "$(date -Is) run=${run_id} attempt=${attempt} failed exit=${exit_code}" >>"$LOG_FILE"
  (( attempt++ ))
  if (( attempt <= MAX_ATTEMPTS )); then
    sleep 15
  fi
done

printf 'failed %s %s\n' "$run_id" "$(date -Is)" >"$STATUS_FILE"
echo "$(date -Is) run=${run_id} failed after ${MAX_ATTEMPTS} attempts" >>"$LOG_FILE"
exit 1
