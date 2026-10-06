from pathlib import Path

ROOT = Path(__file__).parents[1]
compose = (ROOT / "compose.yml").read_text()
override = (ROOT / "compose.override.yml").read_text()

requirements = {
    "database service": "  db:" in compose,
    "backend service": "  backend:" in compose,
    "frontend service": "  frontend:" in compose,
    "database healthcheck": "pg_isready" in compose,
    "API healthcheck": "/api/v1/health" in compose,
    "frontend healthcheck": "fetch('http://localhost:5173')" in compose,
    "migration on startup": "alembic upgrade head" in compose and "alembic upgrade head" in override,
    "same development version": compose.count("${APP_VERSION:-dev}") == 3,
    "deterministic frontend install": "bun install --frozen-lockfile" in compose,
    "healthcheck uses bundled Python": "urllib.request.urlopen" in compose,
}
missing = [name for name, valid in requirements.items() if not valid]
if missing:
    raise SystemExit("Invalid Compose configuration: " + ", ".join(missing))
print("Compose static validation passed: db, migrations, API, frontend, healthchecks and shared APP_VERSION")
