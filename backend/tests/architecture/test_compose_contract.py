import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parents[3]


def test_compose_static_contract() -> None:
    result = subprocess.run([sys.executable, ROOT / "scripts" / "validate-compose.py"], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    assert "Compose static validation passed" in result.stdout
