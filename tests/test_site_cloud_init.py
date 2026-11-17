import importlib
import json
import subprocess
import sys
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "deploy" / "cloud-init" / "render_site_cloud_init.py"
AGENT = importlib.import_module("apps.node_agent.agent")


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
    assert "apt-get update" in content
    assert "DEBIAN_FRONTEND=noninteractive apt-get install -y chrony" in content
    assert "server time.cloudflare.com iburst" in content
    assert "server time.google.com iburst" in content
    assert "DNS=1.1.1.1 8.8.8.8" in content
    assert "Domains=~. lab.example" in content
    assert "chronyd.service" in content
    assert "systemd-resolved.service" in content
    assert "chronyc waitsync 10 0.1" in content
    assert "resolvectl query time.cloudflare.com" in content
    assert "/run/systemd/resolve/resolv.conf /etc/resolv.conf" in content


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



def test_renderer_consumes_the_same_versioned_time_dns_plan_profile(tmp_path):
    profile = {
        "version": "v1",
        "chrony_required": True,
        "resolver_backend": "systemd-resolved",
        "apply_to": ["eph_live_os", "target_os"],
        "ntp_servers": ["time.cloudflare.com"],
        "dns_servers": ["1.1.1.1", "8.8.8.8"],
        "search_domains": ["lab.example"],
        "dns_probe_name": "time.cloudflare.com",
    }
    profile_path = tmp_path / "time-dns-profile.json"
    profile_path.write_text(json.dumps(profile), encoding="utf-8")
    output = tmp_path / "target-user-data.yaml"
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--role",
            "target-os",
            "--profile-json",
            str(profile_path),
            "--output",
            str(output),
        ],
        text=True,
        capture_output=True,
    )
    assert result.returncode == 0, result.stderr
    content = output.read_text(encoding="utf-8")
    assert "server time.cloudflare.com iburst" in content
    assert "DNS=1.1.1.1 8.8.8.8" in content
    assert "Domains=lab.example" in content
    assert "target-os" in content



def test_eph_role_defers_time_dns_execution_until_before_kubeadm(tmp_path):
    profile = {
        "version": "v1",
        "chrony_required": True,
        "resolver_backend": "systemd-resolved",
        "apply_to": ["eph_live_os", "target_os"],
        "ntp_servers": ["time.cloudflare.com"],
        "dns_servers": ["1.1.1.1"],
        "search_domains": [],
        "dns_probe_name": "time.cloudflare.com",
    }
    profile_path = tmp_path / "time-dns-profile.json"
    profile_path.write_text(json.dumps(profile), encoding="utf-8")
    output = tmp_path / "eph-user-data.yaml"
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--role",
            "eph-live-os",
            "--profile-json",
            str(profile_path),
            "--output",
            str(output),
        ],
        text=True,
        capture_output=True,
    )
    assert result.returncode == 0, result.stderr
    content = output.read_text(encoding="utf-8")
    assert "write_files:" in content
    assert "runcmd:" not in content
    assert "ai-agent-site-time-dns-apply" in content



def test_renderer_emits_capi_pre_kubeadm_files_for_target_masters_and_workers(tmp_path):
    profile = {
        "version": "v1",
        "chrony_required": True,
        "resolver_backend": "systemd-resolved",
        "apply_to": ["eph_live_os", "target_os"],
        "ntp_servers": ["time.cloudflare.com"],
        "dns_servers": ["1.1.1.1"],
        "search_domains": ["lab.example"],
        "dns_probe_name": "time.cloudflare.com",
    }
    profile_path = tmp_path / "time-dns-profile.json"
    profile_path.write_text(json.dumps(profile), encoding="utf-8")
    output = tmp_path / "target-kubeadm-fragment.yaml"
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--role",
            "target-os",
            "--profile-json",
            str(profile_path),
            "--format",
            "capi-kubeadm",
            "--output",
            str(output),
        ],
        text=True,
        capture_output=True,
    )
    assert result.returncode == 0, result.stderr
    content = output.read_text(encoding="utf-8")
    assert "files:" in content
    assert "/etc/chrony/chrony.conf" in content
    assert "/etc/systemd/resolved.conf.d/90-ai-agent-site-dns.conf" in content
    assert "/usr/local/sbin/ai-agent-site-time-dns-apply" in content
    assert "preKubeadmCommands:" in content
    assert "chronyc waitsync 10 0.1" in content
    assert "DNS=1.1.1.1" in content
    assert "Domains=~. lab.example" in content
    expected_hash = AGENT.validate_time_dns_profile(profile)["profile_hash"]
    assert f"ai-agent-stack.io/time-dns-profile-hash={expected_hash}" in content
    assert "runcmd:" not in content


def test_capi_kubeadm_format_rejects_eph_live_os_role(tmp_path):
    profile = {
        "version": "v1",
        "chrony_required": True,
        "resolver_backend": "systemd-resolved",
        "apply_to": ["eph_live_os", "target_os"],
        "ntp_servers": ["time.cloudflare.com"],
        "dns_servers": ["1.1.1.1"],
        "search_domains": [],
        "dns_probe_name": "time.cloudflare.com",
    }
    profile_path = tmp_path / "time-dns-profile.json"
    profile_path.write_text(json.dumps(profile), encoding="utf-8")
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--role",
            "eph-live-os",
            "--profile-json",
            str(profile_path),
            "--format",
            "capi-kubeadm",
            "--output",
            str(tmp_path / "invalid.yaml"),
        ],
        text=True,
        capture_output=True,
    )
    assert result.returncode != 0
    assert "CAPI_KUBEADM_FORMAT_REQUIRES_TARGET_OS_ROLE" in result.stderr
