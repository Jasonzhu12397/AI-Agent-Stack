import pathlib
import re
import shutil

MEDIA_ROOT = pathlib.Path("/mnt/eph-bootstrap/network")
TARGET_ROOT = pathlib.Path("/etc/systemd/network")
ALLOWED_SUFFIXES = {".network", ".netdev", ".link"}
FILENAME_PATTERN = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")


def install_network_configuration(media_root=None, target_root=None):
    media_root = pathlib.Path(media_root) if media_root is not None else MEDIA_ROOT
    target_root = pathlib.Path(target_root) if target_root is not None else TARGET_ROOT
    if not media_root.is_dir() or media_root.is_symlink():
        return
    target_root.mkdir(mode=0o755, parents=True, exist_ok=True)
    resolved_media_root = media_root.resolve()
    for source in sorted(media_root.iterdir()):
        if (
            source.suffix not in ALLOWED_SUFFIXES
            or not FILENAME_PATTERN.fullmatch(source.name)
            or source.is_symlink()
            or not source.is_file()
        ):
            continue
        resolved = source.resolve(strict=True)
        if not resolved.is_relative_to(resolved_media_root) or resolved.stat().st_size > 32768:
            raise ValueError("EPH_NETWORK_CONFIG_INVALID:" + source.name)
        destination = target_root / source.name
        shutil.copyfile(str(resolved), str(destination))
        destination.chmod(0o644)


if __name__ == "__main__":
    install_network_configuration()
