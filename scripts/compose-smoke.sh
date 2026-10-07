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
  local exit_code=$?
  if (( exit_code != 0 )); then
    echo "Compose smoke failed; collecting service status and logs before cleanup." >&2
    compose ps --all >&2 || true
    compose logs --no-color >&2 || true
  fi
  if compose down --volumes --remove-orphans; then
    :
  else
    local cleanup_exit_code=$?
    echo "Compose smoke cleanup failed." >&2
    if (( exit_code == 0 )); then
      exit_code=$cleanup_exit_code
    fi
  fi
  exit "$exit_code"
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

compose run --rm --no-deps \
  --volume "$PWD/backend/tests:/app/backend/tests:ro" \
  --env RUN_DB_TESTS=1 \
  backend pytest tests/catalog/test_postgres_title_search.py -q -s --log-cli-level=WARNING

compose run --rm --no-deps \
  --volume "$PWD/backend/tests:/app/backend/tests:ro" \
  --env RUN_DB_TESTS=1 \
  backend pytest \
    tests/data_governance/test_migration_0005_backfill.py \
    tests/data_governance/test_migration_0014_closeout.py \
    -q

compose run --rm --no-deps \
  --volume "$PWD/backend/tests:/app/backend/tests:ro" \
  --volume "$PWD/fixtures:/app/fixtures:ro" \
  --env RUN_DB_TESTS=1 \
  backend pytest \
    tests/data_governance/test_ingestion.py \
    tests/data_governance/test_processing.py \
    -q

echo "Compose smoke passed: API, frontend and seven module schemas are healthy"
