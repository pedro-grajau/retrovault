import hashlib
import sys
from pathlib import Path

root = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else Path(__file__).parents[1]
manifest = root / "contracts" / "api" / "generated.sha256"
errors: list[str] = []
entries: dict[str, str] = {}
for line in manifest.read_text().splitlines():
    if not line or line.count("  ") != 1:
        errors.append(f"manifest: malformed entry {line!r}")
        continue
    expected, relative = line.split("  ", 1)
    if len(expected) != 64 or any(character not in "0123456789abcdef" for character in expected):
        errors.append(f"manifest: invalid checksum for {relative}")
        continue
    if relative in entries:
        errors.append(f"{relative}: duplicate manifest entry")
    entries[relative] = expected

if "frontend/openapi.json" not in entries:
    errors.append("frontend/openapi.json: missing from manifest")

client_prefix = "frontend/src/client/"
expected_client = {relative for relative in entries if relative.startswith(client_prefix)}
if not expected_client:
    errors.append("frontend/src/client: no generated files in manifest")

for relative, expected in entries.items():
    target = root / relative
    actual = hashlib.sha256(target.read_bytes()).hexdigest() if target.is_file() else (
        "directory" if target.is_dir() else "missing"
    )
    if actual != expected:
        errors.append(f"{relative}: expected {expected}, got {actual}")

client_root = root / client_prefix.rstrip("/")
if not client_root.is_dir():
    actual = "file" if client_root.is_file() else "missing"
    errors.append(f"frontend/src/client: expected directory, got {actual}")
else:
    actual_client = {
        path.relative_to(root).as_posix()
        for path in client_root.rglob("*")
        if path.is_file()
    }
    for relative in sorted(actual_client - expected_client):
        errors.append(f"{relative}: untracked generated file")
if errors:
    raise SystemExit("Generated contract drift detected:\n" + "\n".join(errors))
print("Generated OpenAPI/client manifest verified")
