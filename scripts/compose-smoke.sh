#!/usr/bin/env bash
set -euo pipefail

cleanup() {
  docker compose down --volumes --remove-orphans
}
trap cleanup EXIT

docker compose up --detach --wait
curl --fail --silent http://localhost:8000/api/v1/health | grep -q '"status":"ok"'
curl --fail --silent http://localhost:5173 | grep -q "RetroVault"

expected="catalog commerce concierge data_governance platform quality rentals"
actual="$(docker compose exec -T db psql -U postgres -d retrovault -Atc "SELECT string_agg(nspname, ' ' ORDER BY nspname) FROM pg_namespace WHERE nspname IN ('catalog','commerce','concierge','data_governance','platform','quality','rentals');")"
test "$actual" = "$expected"

echo "Compose smoke passed: API, frontend and seven module schemas are healthy"
