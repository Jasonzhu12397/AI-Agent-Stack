#!/usr/bin/env python3
"""Merge two cloud-config documents into a cloud-init MIME multipart user-data."""

import argparse
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path


def merge_cloud_config(base_path, additional_path, output_path):
    paths = [Path(base_path), Path(additional_path)]
    contents = []
    for path in paths:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 262144:
            raise ValueError("CLOUD_INIT_INPUT_INVALID")
        content = path.read_text(encoding="utf-8")
        if not content.lstrip().startswith("#cloud-config"):
            raise ValueError("CLOUD_INIT_INPUT_NOT_CLOUD_CONFIG")
        contents.append(content)

    message = MIMEMultipart("mixed")
    message.attach(MIMEText(contents[0], "cloud-config", "utf-8"))
    message.attach(MIMEText(contents[1], "cloud-config", "utf-8"))
    destination = Path(output_path)
    temporary = destination.with_name(destination.name + ".tmp")
    temporary.write_bytes(message.as_bytes())
    temporary.replace(destination)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", required=True)
    parser.add_argument("--additional", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    try:
        merge_cloud_config(args.base, args.additional, args.output)
    except (OSError, UnicodeError, ValueError) as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
