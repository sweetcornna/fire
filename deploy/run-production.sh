#!/usr/bin/env bash
set -Eeuo pipefail

BASE_DIR="${HUOHUA_BASE_DIR:-/opt/huohua/fire}"
PYTHON_BIN="${HUOHUA_PYTHON_BIN:-/opt/huohua/venv/bin/python}"
LOCK_FILE="${HUOHUA_LOCK_FILE:-/var/lib/huohua/production.lock}"
STATUS_FILE="${HUOHUA_STATUS_FILE:-/var/lib/huohua/last-run.status}"
LOG_FILE="${HUOHUA_RUN_LOG:-/var/log/huohua-run.log}"
MAX_ATTEMPTS="${HUOHUA_MAX_ATTEMPTS:-3}"
RETRY_DELAY_SECONDS="${HUOHUA_RETRY_DELAY_SECONDS:-15}"

case "$MAX_ATTEMPTS" in
  ''|*[!0-9]*)
    echo "HUOHUA_MAX_ATTEMPTS must be a positive integer" >&2
    exit 64
    ;;
esac
if (( MAX_ATTEMPTS < 1 )); then
  echo "HUOHUA_MAX_ATTEMPTS must be a positive integer" >&2
  exit 64
fi
case "$RETRY_DELAY_SECONDS" in
  ''|*[!0-9]*)
    echo "HUOHUA_RETRY_DELAY_SECONDS must be a non-negative integer" >&2
    exit 64
    ;;
esac

if [[ ! -d "$BASE_DIR" ]]; then
  echo "production base directory does not exist: $BASE_DIR" >&2
  exit 66
fi
if [[ ! -x "$PYTHON_BIN" ]]; then
  echo "production Python executable is not executable: $PYTHON_BIN" >&2
  exit 126
fi

umask 077

mkdir -p "$(dirname "$LOCK_FILE")" "$(dirname "$STATUS_FILE")" "$(dirname "$LOG_FILE")"
timestamp() {
  date -u +"%Y-%m-%dT%H:%M:%SZ"
}

# shellcheck disable=SC2329 # invoked through the EXIT trap below
lock_cleanup() {
  if [[ "${HUOHUA_LOCK_MODE:-}" == "mkdir" ]]; then
    rmdir "${LOCK_FILE}.d" 2>/dev/null || true
  fi
}
trap lock_cleanup EXIT

if command -v flock >/dev/null 2>&1; then
  exec 9>"$LOCK_FILE"
  if ! flock -n 9; then
    echo "$(timestamp) another production run is already active" >>"$LOG_FILE"
    exit 0
  fi
else
  # macOS and a few minimal containers do not ship util-linux's flock.
  # mkdir is atomic and is a safe fallback for the single daily job.
  if ! mkdir "${LOCK_FILE}.d" 2>/dev/null; then
    echo "$(timestamp) another production run is already active" >>"$LOG_FILE"
    exit 0
  fi
  export HUOHUA_LOCK_MODE="mkdir"
fi

cd "$BASE_DIR"
run_id="$(date -u +%Y%m%dT%H%M%SZ)-$$"
printf 'running %s %s\n' "$run_id" "$(timestamp)" >"$STATUS_FILE"

attempt=1
while (( attempt <= MAX_ATTEMPTS )); do
  echo "$(timestamp) run=${run_id} attempt=${attempt}/${MAX_ATTEMPTS}" >>"$LOG_FILE"
  if "$PYTHON_BIN" "$BASE_DIR/deploy/production_runner.py" >>"$LOG_FILE" 2>&1; then
    printf 'success %s %s\n' "$run_id" "$(timestamp)" >"$STATUS_FILE"
    echo "$(timestamp) run=${run_id} success" >>"$LOG_FILE"
    exit 0
  else
    exit_code=$?
  fi
  echo "$(timestamp) run=${run_id} attempt=${attempt} failed exit=${exit_code}" >>"$LOG_FILE"
  attempt=$((attempt + 1))
  if (( attempt <= MAX_ATTEMPTS )); then
    sleep "$RETRY_DELAY_SECONDS"
  fi
done

printf 'failed %s %s\n' "$run_id" "$(timestamp)" >"$STATUS_FILE"
echo "$(timestamp) run=${run_id} failed after ${MAX_ATTEMPTS} attempts" >>"$LOG_FILE"
exit 1
