#!/usr/bin/env bash
set -euo pipefail
if [[ -n "${APP_VERSION:-}" ]]; then
  printf '%s\n' "$APP_VERSION"
elif git rev-parse --verify HEAD >/dev/null 2>&1; then
  git rev-parse --short=12 HEAD
else
  printf '%s\n' dev
fi
