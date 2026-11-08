#!/usr/bin/env bash
set -euo pipefail
image=${1:?usage: image-smoke.sh IMAGE}
name="fork-image-smoke-${GITHUB_RUN_ID:-$$}"
cleanup() {
  docker logs "$name" || true
  docker rm -fv "$name" || true
}
trap cleanup EXIT
# Anonymous fresh volume, loopback-only random host port, no agent credentials.
docker run -d --name "$name" -p 127.0.0.1::3100 -v /paperclip \
  -e PAPERCLIP_DEPLOYMENT_MODE=authenticated \
  -e PAPERCLIP_PUBLIC_URL=http://localhost:3100 \
  -e PAPERCLIP_TELEMETRY_DISABLED=1 -e PAPERCLIP_MIGRATION_AUTO_APPLY=true "$image"
port=$(docker port "$name" 3100/tcp | sed 's/.*://')
for attempt in $(seq 1 180); do
  if curl -fsS "http://127.0.0.1:$port/api/health" | grep -q '"status":"ok"'; then
    curl -fsS "http://127.0.0.1:$port/" | grep -qi '<!doctype html'
    exit 0
  fi
  test "$(docker inspect --format '{{.State.Running}}' "$name")" = true
  sleep 2
done
exit 1
