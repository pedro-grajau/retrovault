#!/usr/bin/env bash
set -euo pipefail
root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$root"
uv run --project backend python scripts/verify-generated.py
scratch="$(mktemp -d)"
trap 'rm -rf "$scratch"' EXIT
mkdir -p "$scratch/frontend/src" "$scratch/contracts/api"
cp contracts/api/generated.sha256 "$scratch/contracts/api/generated.sha256"
OPENAPI_OUTPUT_PATH="$scratch/frontend/openapi.json" uv run --project backend python scripts/export-openapi.py
OPENAPI_INPUT="$scratch/frontend/openapi.json" OPENAPI_OUTPUT="$scratch/frontend/src/client" bun run --cwd frontend generate-client
uv run --project backend python scripts/verify-generated.py "$scratch"
uv run --project backend pytest backend/tests/data_governance/test_ingestion.py -q
