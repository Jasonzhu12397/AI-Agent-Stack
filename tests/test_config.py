import os
import subprocess
import sys


def test_production_rejects_default_secret_key() -> None:
    environment = os.environ.copy()
    environment["APP_ENV"] = "production"
    environment["SECRET_KEY"] = "local-only-change-before-production"
    environment["DEBUG"] = "false"
    result = subprocess.run(
        [sys.executable, "-c", "import platform_api.settings"],
        env=environment, capture_output=True, text=True, check=False,
    )
    assert result.returncode != 0
    assert "Production requires DEBUG=false" in result.stderr
