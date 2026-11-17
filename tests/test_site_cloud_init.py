import subprocess
import sys

from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "deploy" / "cloud-init" / "render_site_cloud_init.py"


def _render(tmp_path, *extra):
    output = tmp_path / "user-data.yaml"
    command = [
        sys.executable,
        str(SCRIPT),
        "--role",
        "target-os",
        "--ntp-server",
        "time.cloudflare.com",
        "--dns-server",
        "1.1.1.1",
        "--dns-test-name",
        "time.cloudflare.com",
        "--output",
        str(output),
    ]
    command.extend(extra)
    result = subprocess.run(command, text=True, capture_output=True)
    return result, output


def test_renderer_includes_chrony_and_systemd_resolved_configuration(tmp_path):
    result, output = _render(
        tmp_path,
        "--ntp-server",
        "time.google.com",
        "--dns-server",
        "8.8.8.8",
        "--search-domain",
        "lab.example",
    )
    assert result.returncode == 0, result.stderr
    content = output.read_text(encoding="utf-8")
    assert "package_update: true" in content
    assert "  - chrony" in content
    assert "server time.cloudflare.com iburst" in content
    assert "server time.google.com iburst" in content
    assert "DNS=1.1.1.1 8.8.8.8" in content
    assert "Domains=lab.example" in content
    assert "chronyd.service" in content
    assert "systemd-resolved.service" in content
    assert "chronyc waitsync 10 0.1" in content
    assert "resolvectl query time.cloudflare.com" in content


def test_renderer_rejects_shell_injection_in_ntp_server(tmp_path):
    result, _ = _render(tmp_path, "--ntp-server", "pool.example;touch /tmp/pwned")
    assert result.returncode != 0
    assert "invalid NTP server hostname" in result.stderr


def test_renderer_rejects_non_ip_dns_server(tmp_path):
    result, _ = _render(tmp_path, "--dns-server", "dns.example.net")
    assert result.returncode != 0
    assert "DNS server must be an IPv4 or IPv6 address" in result.stderr


def test_renderer_rejects_shell_injection_in_dns_probe_name(tmp_path):
    result, _ = _render(
        tmp_path,
        "--dns-test-name",
        "time.example;touch /tmp/pwned",
    )
    assert result.returncode != 0
    assert "invalid DNS search domain" in result.stderr


def test_renderer_records_distinct_os_role(tmp_path):
    output = tmp_path / "eph-user-data.yaml"
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--role",
            "eph-live-os",
            "--ntp-server",
            "192.0.2.123",
            "--dns-server",
            "2001:db8::53",
            "--dns-test-name",
            "time.cloudflare.com",
            "--output",
            str(output),
        ],
        text=True,
        capture_output=True,
    )
    assert result.returncode == 0, result.stderr
    assert "eph-live-os" in output.read_text(encoding="utf-8")
