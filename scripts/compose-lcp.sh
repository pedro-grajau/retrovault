#!/usr/bin/env bash
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$root"

cleanup() {
  docker compose stop frontend-lcp >/dev/null 2>&1 || true
}
trap cleanup EXIT

docker compose up -d db backend frontend-lcp
for attempt in $(seq 1 60); do
  if curl --fail --silent --show-error http://127.0.0.1:4173/ >/dev/null; then
    break
  fi
  if [ "$attempt" -eq 60 ]; then
    printf '%s\n' '{"code":"frontend_lcp_not_ready"}' >&2
    exit 2
  fi
  sleep 2
done

export PLAYWRIGHT_BASE_URL="http://127.0.0.1:4173"
export LCP_API_URL="http://127.0.0.1:8000"
bun run --cwd ./frontend measure:lcp
