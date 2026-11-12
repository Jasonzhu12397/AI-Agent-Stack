import pathlib
import runpy

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
    outside.write_text("not copied", encoding="utf-8")
    (media / "99-link.network").symlink_to(outside)

    NETWORK_CONFIG["install_network_configuration"](media, target)

    assert (target / "10-uplink.network").is_file()
    assert not (target / "ignore.txt").exists()
    assert not (target / "99-link.network").exists()


def test_network_profile_installer_rejects_oversized_files(tmp_path):
    media = tmp_path / "media" / "network"
    target = tmp_path / "target"
    media.mkdir(parents=True)
    (media / "10-large.network").write_text("x" * 32769, encoding="utf-8")

    try:
        NETWORK_CONFIG["install_network_configuration"](media, target)
    except ValueError as error:
        assert "EPH_NETWORK_CONFIG_INVALID" in str(error)
    else:
        raise AssertionError("Oversized network profile was accepted")


def test_network_profile_installer_does_not_overwrite_existing_networkd_files(
    tmp_path,
):
    media = tmp_path / "media" / "network"
    target = tmp_path / "target"
    media.mkdir(parents=True)
    target.mkdir()
    (media / "10-uplink.network").write_text("new config", encoding="utf-8")
    existing = target / "10-uplink.network"
    existing.write_text("cloud-init config", encoding="utf-8")

    try:
        NETWORK_CONFIG["install_network_configuration"](media, target)
    except ValueError as error:
        assert "EPH_NETWORK_CONFIG_DESTINATION_EXISTS" in str(error)
    else:
        raise AssertionError("Existing networkd configuration was overwritten")

    assert existing.read_text(encoding="utf-8") == "cloud-init config"


def test_network_profile_installer_rejects_symlinked_target_directory(tmp_path):
    media = tmp_path / "media" / "network"
    target = tmp_path / "target-link"
    actual_target = tmp_path / "actual-target"
    media.mkdir(parents=True)
    actual_target.mkdir()
    target.symlink_to(actual_target, target_is_directory=True)

    try:
        NETWORK_CONFIG["install_network_configuration"](media, target)
    except ValueError as error:
        assert "EPH_NETWORK_CONFIG_TARGET_INVALID" in str(error)
    else:
        raise AssertionError("Symlinked networkd target directory was accepted")
