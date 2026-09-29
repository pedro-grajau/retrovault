#! /usr/bin/env bash

set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$root"

uv run --project backend python scripts/export-openapi.py
bun run --cwd frontend generate-client
uv run --project backend python scripts/update-generated-manifest.py
