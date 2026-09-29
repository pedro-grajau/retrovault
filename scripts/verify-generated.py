import hashlib
import sys
from pathlib import Path

root = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else Path(__file__).parents[1]
manifest = root / "contracts" / "api" / "generated.sha256"
errors: list[str] = []
for line in manifest.read_text().splitlines():
    expected, relative = line.split("  ", 1)
    target = root / relative
    actual = hashlib.sha256(target.read_bytes()).hexdigest() if target.exists() else "missing"
    if actual != expected:
        errors.append(f"{relative}: expected {expected}, got {actual}")
if errors:
    raise SystemExit("Generated contract drift detected:\n" + "\n".join(errors))
print("Generated OpenAPI/client manifest verified")
