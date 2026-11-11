import pathlib
import re
import shutil


MEDIA_ROOT = pathlib.Path("/mnt/eph-bootstrap/network")
TARGET_ROOT = pathlib.Path("/etc/systemd/network")
ALLOWED_SUFFIXES = {".network", ".netdev", ".link"}
FILENAME_PATTERN = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")


def install_network_configuration():
    if not MEDIA_ROOT.is_dir() or MEDIA_ROOT.is_symlink():
        return
    TARGET_ROOT.mkdir(mode=0o755, parents=True, exist_ok=True)
    media_root = MEDIA_ROOT.resolve()
    for source in sorted(MEDIA_ROOT.iterdir()):
        if (
            source.suffix not in ALLOWED_SUFFIXES
            or not FILENAME_PATTERN.fullmatch(source.name)
            or source.is_symlink()
            or not source.is_file()
        ):
            continue
        resolved = source.resolve(strict=True)
        if not resolved.is_relative_to(media_root) or resolved.stat().st_size > 32768:
            raise ValueError("EPH_NETWORK_CONFIG_INVALID:" + source.name)
        destination = TARGET_ROOT / source.name
        shutil.copyfile(str(resolved), str(destination))
        destination.chmod(0o644)


if __name__ == "__main__":
    install_network_configuration()
