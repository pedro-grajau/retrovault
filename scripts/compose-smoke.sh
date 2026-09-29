#!/usr/bin/env bash
set -euo pipefail

command -v bun >/dev/null || {
  echo "Compose smoke requires Bun on the host to run browser tests." >&2
  exit 1
}
test -d node_modules || {
  echo "Compose smoke requires 'bun install --frozen-lockfile' at the repository root." >&2
  exit 1
}

compose() {
  docker compose --project-name "retrovault-smoke-$$" "$@"
}

cleanup() {
  compose down --volumes --remove-orphans
}
trap cleanup EXIT

compose up --detach --wait
curl --fail --silent http://localhost:8000/api/v1/health | grep -q '"status":"ok"'
curl --fail --silent http://localhost:5173 | grep -q "RetroVault"
PLAYWRIGHT_BASE_URL=http://localhost:5173 PLAYWRIGHT_REAL_API=1 bun run --cwd frontend test

expected="catalog commerce concierge data_governance platform quality rentals"
db_user="$(compose exec -T db printenv POSTGRES_USER)"
db_name="$(compose exec -T db printenv POSTGRES_DB)"
actual="$(compose exec -T db psql -U "$db_user" -d "$db_name" -Atc "SELECT string_agg(nspname, ' ' ORDER BY nspname) FROM pg_namespace WHERE nspname IN ('catalog','commerce','concierge','data_governance','platform','quality','rentals');")"
test "$actual" = "$expected"

echo "Compose smoke passed: API, frontend and seven module schemas are healthy"
