#! /usr/bin/env sh

# Exit in case of error
set -e
set -x

compose() {
  docker compose --project-name "retrovault-test-$$" "$@"
}

cleanup() {
  compose down -v --remove-orphans
}
trap cleanup EXIT

compose build
compose down -v --remove-orphans # Remove possibly previous broken stacks left hanging after an error
compose run --rm backend bash scripts/prestart.sh
compose up -d
compose exec -T backend bash scripts/tests-start.sh "$@"
