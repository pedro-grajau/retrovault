#!/usr/bin/env bash

set -e
set -x

FASTAPI_ENV=development RUN_DB_TESTS=1 coverage run -m pytest tests/
coverage report
coverage html --title "${@-coverage}"
