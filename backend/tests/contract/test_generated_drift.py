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
    for relative in ("contracts/api/generated.sha256", "frontend/openapi.json"):
        source = ROOT / relative
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
    shutil.copytree(ROOT / "frontend" / "src" / "client", tmp_path / "frontend" / "src" / "client")
    (tmp_path / "frontend" / "openapi.json").write_text("{}\n")
    result = run_check(tmp_path)
    assert result.returncode != 0
    assert "Generated contract drift detected" in result.stderr


def test_replaced_generated_client_directory_is_reported_as_drift(tmp_path: Path) -> None:
    for relative in ("contracts/api/generated.sha256", "frontend/openapi.json"):
        source = ROOT / relative
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
    client = tmp_path / "frontend" / "src" / "client"
    client.parent.mkdir(parents=True, exist_ok=True)
    client.write_text("replaced directory\n")
    result = run_check(tmp_path)
    assert result.returncode != 0
    assert "frontend/src/client: expected directory, got file" in result.stderr


def test_missing_openapi_manifest_entry_is_reported(tmp_path: Path) -> None:
    shutil.copytree(ROOT / "frontend" / "src" / "client", tmp_path / "frontend" / "src" / "client")
    shutil.copyfile(ROOT / "frontend" / "openapi.json", tmp_path / "frontend" / "openapi.json")
    manifest = (ROOT / "contracts" / "api" / "generated.sha256").read_text()
    target = tmp_path / "contracts" / "api" / "generated.sha256"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("\n".join(line for line in manifest.splitlines() if "frontend/openapi.json" not in line) + "\n")
    result = run_check(tmp_path)
    assert result.returncode != 0
    assert "frontend/openapi.json: missing from manifest" in result.stderr


def test_untracked_generated_client_file_is_reported(tmp_path: Path) -> None:
    for relative in ("contracts/api/generated.sha256", "frontend/openapi.json"):
        source = ROOT / relative
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
    shutil.copytree(ROOT / "frontend" / "src" / "client", tmp_path / "frontend" / "src" / "client")
    (tmp_path / "frontend" / "src" / "client" / "stale.gen.ts").write_text("export {}\n")
    result = run_check(tmp_path)
    assert result.returncode != 0
    assert "frontend/src/client/stale.gen.ts: untracked generated file" in result.stderr
