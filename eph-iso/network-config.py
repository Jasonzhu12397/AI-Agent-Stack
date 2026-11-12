import configparser
import pathlib
import shutil

MEDIA_ROOT = pathlib.Path("/mnt/eph-bootstrap/network")
TARGET_ROOT = pathlib.Path("/etc/systemd/network")
ALLOWED_SUFFIXES = {".network", ".netdev", ".link"}
REQUIRED_SECTIONS = {
    ".network": {"Match"},
    ".netdev": {"NetDev"},
    ".link": {"Match", "Link"},
}


def _validate_network_profile(path):
    try:
        content = path.read_text(encoding="utf-8")
        if not content or "\x00" in content:
            raise ValueError
        parser = configparser.ConfigParser(
            allow_no_value=False,
            delimiters=("=",),
            interpolation=None,
            strict=False,
        )
        parser.optionxform = str
        parser.read_string(content)
    except (OSError, UnicodeError, configparser.Error, ValueError):
        raise ValueError("EPH_NETWORK_CONFIG_SYNTAX_INVALID:" + path.name) from None
    if not REQUIRED_SECTIONS[path.suffix].issubset(parser.sections()):
        raise ValueError("EPH_NETWORK_CONFIG_SECTIONS_INVALID:" + path.name)


def install_network_configuration(media_root=None, target_root=None):
    media_root = pathlib.Path(media_root) if media_root is not None else MEDIA_ROOT
    target_root = pathlib.Path(target_root) if target_root is not None else TARGET_ROOT
    if not media_root.is_dir() or media_root.is_symlink():
        return
    if target_root.is_symlink():
        raise ValueError("EPH_NETWORK_CONFIG_TARGET_INVALID")
    target_root.mkdir(mode=0o755, parents=True, exist_ok=True)
    resolved_media_root = media_root.resolve(strict=True)
    if not target_root.is_dir():
        raise ValueError("EPH_NETWORK_CONFIG_TARGET_INVALID")
    for source in sorted(media_root.iterdir()):
        if source.suffix not in ALLOWED_SUFFIXES:
            continue
        if source.is_symlink():
            raise ValueError("EPH_NETWORK_CONFIG_SYMLINK_REJECTED:" + source.name)
        if not source.is_file():
            continue
        resolved = source.resolve(strict=True)
        if (
            not resolved.is_relative_to(resolved_media_root)
            or resolved.stat().st_size > 32768
        ):
            raise ValueError("EPH_NETWORK_CONFIG_INVALID:" + source.name)
        _validate_network_profile(resolved)
        destination = target_root / source.name
        try:
            with resolved.open("rb") as source_file:
                with destination.open("xb") as destination_file:
                    shutil.copyfileobj(source_file, destination_file)
            destination.chmod(0o644)
        except FileExistsError:
            raise ValueError(
                "EPH_NETWORK_CONFIG_DESTINATION_EXISTS:" + source.name
            ) from None
        except OSError:
            try:
                destination.unlink()
            except FileNotFoundError:
                pass
            raise


if __name__ == "__main__":
    install_network_configuration()
