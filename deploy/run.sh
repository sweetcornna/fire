#!/bin/sh
# Daily spark delivery. Runs on a shared box, so the container is capped to
# leave the other services their memory.
set -u
cd /opt/fire || exit 1

STAMP=$(date +%Y%m%d-%H%M%S)
mkdir -p state logs
exec >>"logs/run-$STAMP.log" 2>&1

echo "=== $(date '+%F %T') 开始 ==="

docker run --rm \
  --name fire-spark \
  --env-file /opt/fire/.env \
  -e DELIVERY_STATE_FILE=/app/state/delivery-state.json \
  -e HUOHUA_COOKIE_PERSIST_FILE=/app/state/cookies.json \
  -v /opt/fire/state:/app/state \
  -v /opt/fire/logs:/app/logs \
  --shm-size=512m \
  --memory=1g --memory-swap=2g \
  fire-spark:latest
STATUS=$?

echo "=== $(date '+%F %T') 结束，退出码 $STATUS ==="

# The state file is what stops a rerun from messaging the same people twice;
# it is keyed by date, so yesterday's entries only take up space.
find /opt/fire/logs -name 'run-*.log' -mtime +14 -delete 2>/dev/null
exit $STATUS
