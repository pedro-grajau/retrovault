import hashlib
from pathlib import Path


ROOT = Path(__file__).parents[1]
MANIFEST = ROOT / "contracts" / "api" / "generated.sha256"
OPENAPI = ROOT / "frontend" / "openapi.json"
CLIENT_DIRECTORY = ROOT / "frontend" / "src" / "client"


def generated_files() -> list[Path]:
    if not OPENAPI.is_file():
        raise SystemExit(f"Generated OpenAPI is missing: {OPENAPI.relative_to(ROOT)}")
    if not CLIENT_DIRECTORY.is_dir():
        raise SystemExit(
            f"Generated client directory is missing: {CLIENT_DIRECTORY.relative_to(ROOT)}"
        )
    return [OPENAPI, *sorted(path for path in CLIENT_DIRECTORY.rglob("*") if path.is_file())]


entries = [
    f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.relative_to(ROOT).as_posix()}"
    for path in generated_files()
]
MANIFEST.write_text("\n".join(entries) + "\n")
print(f"Updated generated contract manifest with {len(entries)} files")
