#!/usr/bin/env python
import os
import sys
from pathlib import Path


def main() -> None:
    repository_root = Path(__file__).resolve().parents[2]
    if str(repository_root) not in sys.path:
        sys.path.insert(0, str(repository_root))
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "platform_api.settings")
    from django.core.management import execute_from_command_line
    execute_from_command_line(sys.argv)


if __name__ == "__main__":
    main()
