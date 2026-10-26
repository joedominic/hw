#!/usr/bin/env bash
# Provision Redis #2 for JobApp-Main on port 6380 (Docker).
# Run on the Pi (192.168.2.174) as a user in the docker group.
set -euo pipefail

NAME="${REDIS_JOBAPP_NAME:-redis-jobapp}"
PORT="${REDIS_JOBAPP_PORT:-6380}"
IMAGE="${REDIS_JOBAPP_IMAGE:-redis:7.0-alpine}"
MAXMEM="${REDIS_JOBAPP_MAXMEMORY:-512mb}"

if docker ps -a --format '{{.Names}}' | grep -qx "$NAME"; then
  echo "Container $NAME already exists — starting if needed"
  docker start "$NAME" >/dev/null
else
  echo "Creating $NAME on host port $PORT ($MAXMEM, noeviction)"
  docker run -d \
    --name "$NAME" \
    --restart unless-stopped \
    -p "${PORT}:6379" \
    redis:7.0-alpine \
    redis-server \
      --maxmemory "$MAXMEM" \
      --maxmemory-policy noeviction \
      --appendonly yes \
      --protected-mode no
fi

echo "Waiting for PONG..."
for i in $(seq 1 20); do
  if docker exec "$NAME" redis-cli ping 2>/dev/null | grep -q PONG; then
    echo "OK: redis://127.0.0.1:${PORT} (from Pi) / redis://192.168.2.174:${PORT} (from LAN)"
    docker exec "$NAME" redis-cli CONFIG GET maxmemory
    docker exec "$NAME" redis-cli CONFIG GET maxmemory-policy
    exit 0
  fi
  sleep 0.5
done
echo "Redis did not become ready" >&2
exit 1
