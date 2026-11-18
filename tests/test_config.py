import os
import subprocess
import sys
from pathlib import Path


def test_production_rejects_default_secret_key() -> None:
    environment = os.environ.copy()
    environment["APP_ENV"] = "production"
    environment["SECRET_KEY"] = "local-only-change-before-production"
    environment["DEBUG"] = "false"
    repository_root = Path(__file__).resolve().parents[1]
    python_paths = [
        str(repository_root / "apps/platform-api"),
        str(repository_root),
    ]
    existing_pythonpath = environment.get("PYTHONPATH")
    if existing_pythonpath:
        python_paths.append(existing_pythonpath)
    environment["PYTHONPATH"] = os.pathsep.join(python_paths)
    result = subprocess.run(
        [sys.executable, "-c", "import platform_api.settings"],
        env=environment, capture_output=True, text=True, check=False,
    )
    assert result.returncode != 0
    assert "Production requires DEBUG=false" in result.stderr
