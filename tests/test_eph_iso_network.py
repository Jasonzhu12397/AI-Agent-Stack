import pathlib
import runpy

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "eph-iso/network-config.py"
NETWORK_CONFIG = runpy.run_path(str(MODULE_PATH))


def test_network_profile_installer_copies_only_supported_regular_files(tmp_path):
    media = tmp_path / "media" / "network"
    target = tmp_path / "target"
    media.mkdir(parents=True)
    (media / "10-uplink.network").write_text(
        "[Match]\nName=enp5s0\n\n[Network]\nDHCP=ipv4\n",
        encoding="utf-8",
    )
    (media / "ignore.txt").write_text("ignored", encoding="utf-8")
    outside = tmp_path / "outside.network"
    outside.write_text("[Match]\nName=*\n[Network]\nDHCP=yes\n", encoding="utf-8")
    (media / "99-link.network").symlink_to(outside)

    with pytest.raises(ValueError, match="EPH_NETWORK_CONFIG_SYMLINK_REJECTED"):
        NETWORK_CONFIG["install_network_configuration"](media, target)

    assert (target / "10-uplink.network").is_file()
    assert not (target / "ignore.txt").exists()
    assert not (target / "99-link.network").exists()


def test_network_profile_installer_rejects_oversized_files(tmp_path):
    media = tmp_path / "media" / "network"
    target = tmp_path / "target"
    media.mkdir(parents=True)
    (media / "10-large.network").write_text("x" * 32769, encoding="utf-8")

    with pytest.raises(ValueError, match="EPH_NETWORK_CONFIG_INVALID"):
        NETWORK_CONFIG["install_network_configuration"](media, target)


@pytest.mark.parametrize(
    "filename,content",
    [
        ("10-invalid.network", "[Match\nName=enp5s0\n[Network]\nDHCP=yes\n"),
        ("10-missing.network", "[Match]\nName=enp5s0\n"),
        ("20-invalid.netdev", "[Match]\nName=br0\n"),
        ("30-invalid.link", "[Match]\nOriginalName=eth*\n"),
    ],
)
def test_network_profile_installer_rejects_malformed_profiles(
    tmp_path, filename, content
):
    media = tmp_path / "media" / "network"
    target = tmp_path / "target"
    media.mkdir(parents=True)
    (media / filename).write_text(content, encoding="utf-8")

    with pytest.raises(ValueError, match="EPH_NETWORK_CONFIG_(SYNTAX|SECTIONS)_INVALID"):
        NETWORK_CONFIG["install_network_configuration"](media, target)

    assert not (target / filename).exists()


def test_network_profile_installer_does_not_overwrite_existing_networkd_files(
    tmp_path,
):
    media = tmp_path / "media" / "network"
    target = tmp_path / "target"
    media.mkdir(parents=True)
    target.mkdir()
    (media / "10-uplink.network").write_text(
        "[Match]\nName=enp5s0\n[Network]\nDHCP=ipv4\n",
        encoding="utf-8",
    )
    existing = target / "10-uplink.network"
    existing.write_text("cloud-init config", encoding="utf-8")

    with pytest.raises(ValueError, match="EPH_NETWORK_CONFIG_DESTINATION_EXISTS"):
        NETWORK_CONFIG["install_network_configuration"](media, target)

    assert existing.read_text(encoding="utf-8") == "cloud-init config"


def test_network_profile_installer_rejects_symlinked_target_directory(tmp_path):
    media = tmp_path / "media" / "network"
    target = tmp_path / "target-link"
    actual_target = tmp_path / "actual-target"
    media.mkdir(parents=True)
    actual_target.mkdir()
    target.symlink_to(actual_target, target_is_directory=True)

    with pytest.raises(ValueError, match="EPH_NETWORK_CONFIG_TARGET_INVALID"):
        NETWORK_CONFIG["install_network_configuration"](media, target)
