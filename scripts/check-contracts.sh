#!/usr/bin/env bash
set -euo pipefail
root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$root"
python scripts/verify-generated.py
uv run --project backend python scripts/export-openapi.py
bun run --cwd frontend generate-client
python scripts/verify-generated.py
