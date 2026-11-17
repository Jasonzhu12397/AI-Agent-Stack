import email
import pathlib
import runpy

ROOT = pathlib.Path(__file__).resolve().parents[1]
MERGER = runpy.run_path(str(ROOT / "eph-iso/merge-cloud-init-user-data.py"))


def test_merge_cloud_config_preserves_bootstrap_and_adds_site_time_dns(tmp_path):
    base = tmp_path / "user-data"
    additional = tmp_path / "time-dns-user-data"
    output = tmp_path / "merged-user-data"
    base.write_text(
        "#cloud-config\nwrite_files:\n  - path: /etc/eph/bootstrap.json\n",
        encoding="utf-8",
    )
    additional.write_text(
        "#cloud-config\nwrite_files:\n  - path: /etc/chrony.conf\n",
        encoding="utf-8",
    )

    MERGER["merge_cloud_config"](base, additional, output)

    message = email.message_from_bytes(output.read_bytes())
    assert message.is_multipart()
    parts = message.get_payload()
    assert len(parts) == 2
    assert all(part.get_content_type() == "text/cloud-config" for part in parts)
    assert "/etc/eph/bootstrap.json" in parts[0].get_payload(decode=True).decode("utf-8")
    assert "/etc/chrony.conf" in parts[1].get_payload(decode=True).decode("utf-8")


def test_merge_cloud_config_rejects_non_cloud_config_input(tmp_path):
    base = tmp_path / "user-data"
    additional = tmp_path / "time-dns-user-data"
    base.write_text("#!/bin/sh\necho unsafe\n", encoding="utf-8")
    additional.write_text("#cloud-config\nwrite_files: []\n", encoding="utf-8")

    try:
        MERGER["merge_cloud_config"](base, additional, tmp_path / "out")
    except ValueError as exc:
        assert str(exc) == "CLOUD_INIT_INPUT_NOT_CLOUD_CONFIG"
    else:
        raise AssertionError("non-cloud-config input was accepted")
