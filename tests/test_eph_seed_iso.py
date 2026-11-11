# ruff: noqa: I001
import os
import pathlib
import subprocess
import zipfile

import pytest


ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "eph-iso/create-seed-iso.sh"


def create_seed_archive(path, entries=None):
    values = entries or {
        "user-data": "#cloud-config\n{}\n",
        "meta-data": "instance-id: eph-test\n",
        "network-config": "version: 2\n",
    }
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in values.items():
            archive.writestr(name, content)


def test_seed_iso_builder_creates_private_cidata_image(tmp_path):
    archive = tmp_path / "seed.zip"
    output = tmp_path / "seed.iso"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    xorriso = fake_bin / "xorriso"
    xorriso.write_text(
        "#!/bin/sh\n"
        "while [ \"$#\" -gt 0 ]; do\n"
        "  if [ \"$1\" = \"-o\" ]; then\n"
        "    shift\n"
        "    printf 'fake-iso' > \"$1\"\n"
        "  fi\n"
        "  shift\n"
        "done\n",
        encoding="utf-8",
    )
    xorriso.chmod(0o755)
    create_seed_archive(archive)
    environment = dict(os.environ)
    environment["PATH"] = str(fake_bin) + os.pathsep + environment["PATH"]

    result = subprocess.run(
        ["bash", str(SCRIPT), str(archive), str(output)],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )

    assert result.returncode == 0, result.stderr
    assert output.read_bytes() == b"fake-iso"
    assert output.stat().st_mode & 0o777 == 0o600
    assert (tmp_path / "seed.iso.sha256").stat().st_mode & 0o777 == 0o600
    assert "CIDATA" in result.stdout or result.stdout == ""


@pytest.mark.parametrize(
    "entries",
    [
        {"user-data": "x", "meta-data": "y", "network-config": "z", "extra": "bad"},
        {"../user-data": "x", "meta-data": "y", "network-config": "z"},
        {"user-data": "x", "meta-data": "y"},
    ],
)
def test_seed_iso_builder_rejects_unexpected_archive_entries(tmp_path, entries):
    archive = tmp_path / "seed.zip"
    output = tmp_path / "seed.iso"
    create_seed_archive(archive, entries)

    result = subprocess.run(
        ["bash", str(SCRIPT), str(archive), str(output)],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert not output.exists()
    assert "SEED_ARCHIVE_INVALID" in result.stderr


def test_seed_iso_builder_refuses_to_overwrite_output(tmp_path):
    archive = tmp_path / "seed.zip"
    output = tmp_path / "seed.iso"
    create_seed_archive(archive)
    output.write_text("existing", encoding="utf-8")

    result = subprocess.run(
        ["bash", str(SCRIPT), str(archive), str(output)],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert output.read_text(encoding="utf-8") == "existing"
    assert "SEED_ISO_OUTPUT_ALREADY_EXISTS" in result.stderr
