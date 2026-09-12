#!/usr/bin/env bash
# Dispatch the production dev workflow once a day from the server cron.
set -Eeuo pipefail

TOKEN_FILE="${HUOHUA_GITHUB_TOKEN_FILE:-/root/.huohua_token}"
LOG_FILE="${HUOHUA_TRIGGER_LOG:-/root/huohua_trigger.log}"
LOCK_FILE="${HUOHUA_TRIGGER_LOCK:-/var/lock/huohua-trigger.lock}"
REPOSITORY="${HUOHUA_GITHUB_REPOSITORY:-sweetcornna/fire}"
WORKFLOW="${HUOHUA_GITHUB_WORKFLOW:-schedule_dev.yml}"
REF="${HUOHUA_GITHUB_REF:-dev}"

if [[ ! -r "$TOKEN_FILE" ]]; then
  echo "GitHub token file is missing or unreadable: $TOKEN_FILE" >&2
  exit 66
fi

mkdir -p "$(dirname "$LOG_FILE")" "$(dirname "$LOCK_FILE")"
exec 9>"$LOCK_FILE"
if ! flock -n 9; then
  echo "$(date -Is) another workflow dispatch is already active" >>"$LOG_FILE"
  exit 0
fi

token="$(tr -d '[:space:]' <"$TOKEN_FILE")"
if [[ -z "$token" ]]; then
  echo "GitHub token file is empty: $TOKEN_FILE" >&2
  exit 65
fi

response_file="$(mktemp)"
trap 'rm -f "$response_file"' EXIT
now="$(date -Is)"
endpoint="https://api.github.com/repos/${REPOSITORY}/actions/workflows/${WORKFLOW}/dispatches"

http_code="$(curl --fail-with-body --silent --show-error --retry 3 --retry-delay 5 \
  --output "$response_file" --write-out "%{http_code}" \
  -X POST \
  -H "Accept: application/vnd.github+json" \
  -H "Authorization: Bearer $token" \
  -H "X-GitHub-Api-Version: 2022-11-28" \
  -H "Content-Type: application/json" \
  "$endpoint" \
  --data "{\"ref\":\"$REF\"}")" || true

if [[ "$http_code" == "204" ]]; then
  echo "[$now] OK (204) repository=$REPOSITORY workflow=$WORKFLOW ref=$REF" >>"$LOG_FILE"
  exit 0
fi

{
  echo "[$now] FAIL HTTP=${http_code:-curl-error} repository=$REPOSITORY workflow=$WORKFLOW ref=$REF body:"
  cat "$response_file"
  echo
} >>"$LOG_FILE"
exit 1
