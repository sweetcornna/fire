#!/usr/bin/env bash
# Dispatch the production workflow once a day from the server cron.
set -Eeuo pipefail

DRY_RUN=0
if [[ "${1:-}" == "--dry-run" ]]; then
  DRY_RUN=1
  shift
fi
if (( $# > 0 )); then
  echo "usage: $0 [--dry-run]" >&2
  exit 64
fi

TOKEN_FILE="${HUOHUA_GITHUB_TOKEN_FILE:-/root/.fire_gh_token}"
LOG_FILE="${HUOHUA_TRIGGER_LOG:-/root/fire_trigger.log}"
LOCK_FILE="${HUOHUA_TRIGGER_LOCK:-/var/lock/fire-trigger.lock}"
REPOSITORY="${HUOHUA_GITHUB_REPOSITORY:-sweetcornna/fire}"
WORKFLOW="${HUOHUA_GITHUB_WORKFLOW:-schedule.yml}"
REF="${HUOHUA_GITHUB_REF:-main}"
WAIT_SECONDS="${HUOHUA_WAIT_SECONDS:-2700}"
POLL_SECONDS="${HUOHUA_POLL_SECONDS:-15}"
DISPATCH_ATTEMPTS="${HUOHUA_DISPATCH_ATTEMPTS:-3}"
DISPATCH_RETRY_SECONDS="${HUOHUA_DISPATCH_RETRY_SECONDS:-30}"

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
case "$WAIT_SECONDS" in
  ''|*[!0-9]*) echo "HUOHUA_WAIT_SECONDS must be a non-negative integer" >&2; exit 64 ;;
esac
case "$POLL_SECONDS" in
  ''|*[!0-9]*) echo "HUOHUA_POLL_SECONDS must be a positive integer" >&2; exit 64 ;;
esac
if (( POLL_SECONDS < 1 )); then
  echo "HUOHUA_POLL_SECONDS must be a positive integer" >&2
  exit 64
fi
case "$DISPATCH_ATTEMPTS" in
  ''|*[!0-9]*) echo "HUOHUA_DISPATCH_ATTEMPTS must be a positive integer" >&2; exit 64 ;;
esac
if (( DISPATCH_ATTEMPTS < 1 )); then
  echo "HUOHUA_DISPATCH_ATTEMPTS must be a positive integer" >&2
  exit 64
fi
case "$DISPATCH_RETRY_SECONDS" in
  ''|*[!0-9]*) echo "HUOHUA_DISPATCH_RETRY_SECONDS must be a non-negative integer" >&2; exit 64 ;;
esac

if ! command -v curl >/dev/null 2>&1 || ! command -v jq >/dev/null 2>&1; then
  echo "curl and jq are required to trigger the production workflow" >&2
  exit 69
fi

response_file="$(mktemp)"
trap 'rm -f "$response_file"' EXIT
now="$(date -Is)"
dispatch_epoch="$(date +%s)"
endpoint="https://api.github.com/repos/${REPOSITORY}/actions/workflows/${WORKFLOW}/dispatches"

payload="$(jq -nc --arg ref "$REF" '{ref: $ref}')"
if (( DRY_RUN )); then
  echo "dry-run repository=$REPOSITORY workflow=$WORKFLOW ref=$REF"
  exit 0
fi

dispatch_ok=0
for dispatch_attempt in $(seq 1 "$DISPATCH_ATTEMPTS"); do
  : >"$response_file"
  http_code="$(curl --fail-with-body --silent --show-error --retry 3 --retry-delay 5 \
    --output "$response_file" --write-out "%{http_code}" \
    -X POST \
    -H "Accept: application/vnd.github+json" \
    -H "Authorization: Bearer $token" \
    -H "X-GitHub-Api-Version: 2022-11-28" \
    -H "Content-Type: application/json" \
    "$endpoint" \
    --data "$payload")" || true
  if [[ "$http_code" == "204" ]]; then
    dispatch_ok=1
    echo "[$now] OK (204) repository=$REPOSITORY workflow=$WORKFLOW ref=$REF attempt=$dispatch_attempt; waiting for run" >>"$LOG_FILE"
    break
  fi
  echo "[$(date -Is)] dispatch attempt=${dispatch_attempt}/${DISPATCH_ATTEMPTS} failed HTTP=${http_code:-curl-error}" >>"$LOG_FILE"
  if (( dispatch_attempt < DISPATCH_ATTEMPTS )); then
    sleep "$DISPATCH_RETRY_SECONDS"
  fi
done
if (( dispatch_ok == 0 )); then
  {
    echo "[$(date -Is)] FAIL dispatch repository=$REPOSITORY workflow=$WORKFLOW ref=$REF body:"
    cat "$response_file"
    echo
  } >>"$LOG_FILE"
  exit 1
fi

if (( WAIT_SECONDS == 0 )); then
  exit 0
fi

runs_endpoint="https://api.github.com/repos/${REPOSITORY}/actions/workflows/${WORKFLOW}/runs?branch=${REF}&event=workflow_dispatch&per_page=20"
elapsed=0
while (( elapsed <= WAIT_SECONDS )); do
  runs_file="$(mktemp)"
  runs_code="$(curl --fail-with-body --silent --show-error --retry 2 --retry-delay 3 \
    --output "$runs_file" --write-out "%{http_code}" \
    -H "Accept: application/vnd.github+json" \
    -H "Authorization: Bearer $token" \
    -H "X-GitHub-Api-Version: 2022-11-28" \
    "$runs_endpoint")" || true

  if [[ "$runs_code" == "200" ]]; then
    run_row="$(jq -r --argjson started "$dispatch_epoch" '
      [.workflow_runs[]
       | select((.created_at | fromdateiso8601) >= $started)
       | {id, status, conclusion, html_url}
      ]
      | sort_by(.id)
      | last
      | if . then [.id, .status, (.conclusion // ""), .html_url] | @tsv else "" end
    ' "$runs_file")"
    if [[ -n "$run_row" ]]; then
      IFS=$'\t' read -r run_id run_status run_conclusion run_url <<<"$run_row"
      if [[ "$run_status" == "completed" ]]; then
        if [[ "$run_conclusion" == "success" ]]; then
          echo "[$(date -Is)] OK workflow_run=$run_id conclusion=success url=$run_url" >>"$LOG_FILE"
          rm -f "$runs_file"
          exit 0
        fi
        echo "[$(date -Is)] FAIL workflow_run=$run_id conclusion=${run_conclusion:-unknown} url=$run_url" >>"$LOG_FILE"
        rm -f "$runs_file"
        exit 1
      fi
    fi
  fi
  rm -f "$runs_file"
  sleep "$POLL_SECONDS"
  elapsed=$((elapsed + POLL_SECONDS))
done

echo "[$(date -Is)] FAIL workflow run did not complete within ${WAIT_SECONDS}s" >>"$LOG_FILE"
exit 1
