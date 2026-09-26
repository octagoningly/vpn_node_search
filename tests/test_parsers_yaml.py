from __future__ import annotations

from pathlib import Path

import yaml

from nodebench.parsers.yaml_clash import parse_clash_yaml

PROJECT_ROOT = Path(__file__).resolve().parents[1]

SRC = "src-yaml"

VLESS_UUID = "123e4567-e89b-12d3-a456-426614174000"


def dump(document) -> str:
    return yaml.safe_dump(document, allow_unicode=True, sort_keys=False)


def entry(**overrides):
    base = {
        "type": "trojan",
        "server": "192.0.2.14",
        "port": 443,
        "password": "sample-password",
        "name": "sample-node",
    }
    base.update(overrides)
    return base


def test_sample_clash_mapping():
    text = (PROJECT_ROOT / "input" / "sample-clash.yaml").read_text(encoding="utf-8")
    proxies, issues = parse_clash_yaml(text, SRC)
    assert issues == []
    assert len(proxies) == 2
    first = proxies[0]
    assert first.source_id == SRC
    assert first.protocol == "vless"
    assert first.server == "192.0.2.30"
    assert first.port == 443
    assert first.transport == "ws"
    assert first.security == "tls"
    assert first.secrets == {"uuid": VLESS_UUID}
    assert first.params == {
        "path": "/ws",
        "host": "node.example.test",
        "sni": "node.example.test",
    }
    assert first.remarks == "sample-192-0-2-30"
    second = proxies[1]
    assert second.protocol == "trojan"
    assert second.server == "203.0.113.30"
    assert second.transport == "tcp"
    assert second.security == "tls"
    assert second.secrets == {"password": "sample-password"}
    assert second.params == {"sni": "edge.example.test"}
    assert second.remarks == "sample-203-0-113-30"


def test_unknown_type_reported():
    text = dump({"proxies": [entry(type="quantum")]})
    proxies, issues = parse_clash_yaml(text, SRC)
    assert proxies == []
    assert len(issues) == 1
    assert issues[0].code == "unsupported_protocol"
    assert "quantum" in issues[0].message_redacted
    assert issues[0].raw_ref == "proxies:0"


def test_missing_server_reported():
    text = dump({"proxies": [entry(server=None)]})
    proxies, issues = parse_clash_yaml(text, SRC)
    assert proxies == []
    assert issues[0].code == "missing_server"
    assert issues[0].raw_ref == "proxies:0"


def test_invalid_port_reported():
    for bad_port in ("abc", 0, 65536):
        text = dump({"proxies": [entry(port=bad_port)]})
        proxies, issues = parse_clash_yaml(text, SRC)
        assert proxies == [], bad_port
        assert issues[0].code == "invalid_port", bad_port


def test_missing_password_reported():
    document = {"proxies": [{"type": "trojan", "server": "192.0.2.14", "port": 443}]}
    proxies, issues = parse_clash_yaml(dump(document), SRC)
    assert proxies == []
    assert issues[0].code == "missing_credentials"


def test_missing_proxies_key_reported():
    proxies, issues = parse_clash_yaml("url: https://example.test/docs\n", SRC)
    assert proxies == []
    assert issues[0].code == "missing_proxies"


def test_empty_proxies_list_reported():
    proxies, issues = parse_clash_yaml(dump({"proxies": []}), SRC)
    assert proxies == []
    assert issues[0].code == "missing_proxies"


def test_non_mapping_entry_reported():
    proxies, issues = parse_clash_yaml(dump({"proxies": [42]}), SRC)
    assert proxies == []
    assert issues[0].code == "invalid_yaml"
    assert issues[0].raw_ref == "proxies:0"


def test_deep_nesting_rejected():
    chain = {}
    node = chain
    for _ in range(30):
        node["chain"] = {}
        node = node["chain"]
    document = {
        "proxies": [
            entry(nested={"chain": chain}),
        ]
    }
    proxies, issues = parse_clash_yaml(dump(document), SRC)
    assert proxies == []
    assert issues[0].code == "invalid_yaml"


def test_proxy_limit_truncates():
    document = {"proxies": [entry(name="sample-{0}".format(i)) for i in range(2005)]}
    proxies, issues = parse_clash_yaml(dump(document), SRC)
    assert len(proxies) == 2000
    assert [issue.code for issue in issues] == ["proxy_limit_exceeded"]
    assert issues[0].raw_ref == "proxies"


def test_file_too_large_rejected():
    payload = "中" * (2 * 1024 * 1024)
    proxies, issues = parse_clash_yaml(payload, SRC)
    assert proxies == []
    assert issues[0].code == "file_too_large"


def test_broken_yaml_reports_class_name_only():
    proxies, issues = parse_clash_yaml("proxies: [unterminated\n", SRC)
    assert proxies == []
    assert issues[0].code == "invalid_yaml"
    message = issues[0].message_redacted
    assert message.endswith("Error")
    assert " " not in message
    assert "unterminated" not in message


def test_reality_options_promoted():
    document = {
        "proxies": [
            {
                "type": "vless",
                "server": "192.0.2.16",
                "port": 443,
                "uuid": VLESS_UUID,
                "tls": True,
                "network": "tcp",
                "reality-opts": {
                    "public-key": "sample-public-key",
                    "short-id": "0123456789abcdef",
                },
            }
        ]
    }
    proxies, issues = parse_clash_yaml(dump(document), SRC)
    assert issues == []
    assert proxies[0].security == "reality"
    assert proxies[0].params["reality-opts"]["public-key"] == "sample-public-key"
    assert proxies[0].remarks == ""


def test_alpn_list_and_alter_id():
    document = {
        "proxies": [
            {
                "type": "vmess",
                "server": "198.51.100.15",
                "port": 443,
                "uuid": VLESS_UUID,
                "alterId": 2,
                "alpn": ["h2", "http/1.1"],
                "cipher": "auto",
                "network": "ws",
            }
        ]
    }
    proxies, issues = parse_clash_yaml(dump(document), SRC)
    assert issues == []
    node = proxies[0]
    assert node.params["alter_id"] == 2
    assert node.params["alpn"] == "h2,http/1.1"
    assert node.params["cipher"] == "auto"


def test_ss_cipher_stored_in_secrets():
    document = {
        "proxies": [
            {
                "type": "ss",
                "server": "203.0.113.15",
                "port": 8388,
                "cipher": "aes-256-gcm",
                "password": "sample-password",
            }
        ]
    }
    proxies, issues = parse_clash_yaml(dump(document), SRC)
    assert issues == []
    node = proxies[0]
    assert node.secrets == {"password": "sample-password", "cipher": "aes-256-gcm"}
    assert "cipher" not in node.params


def test_grpc_service_name_promoted():
    document = {
        "proxies": [
            {
                "type": "vless",
                "server": "192.0.2.17",
                "port": 443,
                "uuid": VLESS_UUID,
                "network": "grpc",
                "tls": True,
                "grpc-opts": {"grpc-service-name": "sample-service"},
            }
        ]
    }
    proxies, issues = parse_clash_yaml(dump(document), SRC)
    assert issues == []
    assert proxies[0].transport == "grpc"
    assert proxies[0].params["serviceName"] == "sample-service"


def test_unknown_top_level_keys_kept_in_params():
    text = (
        "proxies:\n"
        "  - type: trojan\n"
        "    server: 192.0.2.18\n"
        "    port: 443\n"
        "    password: sample-password\n"
        "    custom-flag: sample-value\n"
    )
    proxies, issues = parse_clash_yaml(text, SRC)
    assert issues == []
    assert proxies[0].params["custom-flag"] == "sample-value"
