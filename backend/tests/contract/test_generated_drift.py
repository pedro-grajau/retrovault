import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parents[3]


def run_check(root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, ROOT / "scripts" / "verify-generated.py", root], capture_output=True, text=True, check=False)


def test_committed_openapi_and_client_have_no_drift() -> None:
    result = run_check(ROOT)
    assert result.returncode == 0, result.stderr


def test_drift_is_reported_without_silent_regeneration(tmp_path: Path) -> None:
    for relative in ("contracts/api/generated.sha256", "frontend/openapi.json", "frontend/src/client/client.gen.ts", "frontend/src/client/index.ts", "frontend/src/client/sdk.gen.ts", "frontend/src/client/types.gen.ts"):
        source = ROOT / relative
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
    (tmp_path / "frontend" / "openapi.json").write_text("{}\n")
    result = run_check(tmp_path)
    assert result.returncode != 0
    assert "Generated contract drift detected" in result.stderr
