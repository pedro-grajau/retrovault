import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "backend"))
from app.main import app

target = Path(os.environ.get("OPENAPI_OUTPUT_PATH", Path(__file__).parents[1] / "frontend" / "openapi.json"))
target.parent.mkdir(parents=True, exist_ok=True)
target.write_text(json.dumps(app.openapi(), indent=2, sort_keys=True) + "\n")
