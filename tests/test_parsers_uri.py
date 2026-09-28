from __future__ import annotations

import base64
import json

from nodebench.parsers.common import ERROR_CODES
from nodebench.parsers.uri import parse_uri

SRC = "src-uri"

VLESS_UUID = "123e4567-e89b-12d3-a456-426614174000"


def test_vless_parses_full_fields():
    line = (
        "vless://123e4567-e89b-12d3-a456-426614174000@192.0.2.10:443"
        "?type=tcp&security=tls&host=node.example.test#sample-192-0-2-10"
    )
    node, issue = parse_uri(line, SRC)
    assert issue is None
    assert node is not None
    assert node.source_id == SRC
    assert node.protocol == "vless"
    assert node.server == "192.0.2.10"
    assert node.port == 443
    assert node.transport == "tcp"
    assert node.security == "tls"
    assert node.secrets == {"uuid": VLESS_UUID}
    assert node.params == {"host": "node.example.test"}
    assert node.remarks == "sample-192-0-2-10"
    assert issue is None or issue.code in ERROR_CODES


def test_vless_query_credentials_stay_out_of_params():
    line = "vless://123e4567-e89b-12d3-a456-426614174000@192.0.2.11:443?uuid=other#r"
    node, issue = parse_uri(line, SRC)
    assert issue is None
    assert "uuid" not in node.params
    assert node.secrets["uuid"] == VLESS_UUID


def test_vless_missing_uuid():
    node, issue = parse_uri("vless://@192.0.2.11:443#r", SRC)
    assert node is None
    assert issue is not None
    assert issue.code == "missing_credentials"
    assert issue.source_id == SRC


def test_vmess_json_payload():
    payload = {
        "add": "198.51.100.10",
        "port": "443",
        "id": VLESS_UUID,
        "ps": "sample-vmess",
        "net": "ws",
        "tls": "tls",
        "aid": "0",
        "scy": "auto",
        "path": "/ws",
    }
    authority = base64.b64encode(json.dumps(payload).encode("utf-8")).decode("ascii")
    node, issue = parse_uri("vmess://{0}#ignored".format(authority), SRC)
    assert issue is None
    assert node is not None
    assert node.protocol == "vmess"
    assert node.server == "198.51.100.10"
    assert node.port == 443
    assert node.transport == "ws"
    assert node.security == "tls"
    assert node.secrets == {"uuid": VLESS_UUID}
    assert node.params["cipher"] == "auto"
    assert node.params["path"] == "/ws"
    assert "alter_id" not in node.params
    assert node.remarks == "sample-vmess"


def test_vmess_missing_server():
    authority = base64.b64encode(
        json.dumps({"port": "443", "id": VLESS_UUID}).encode("utf-8")
    ).decode("ascii")
    node, issue = parse_uri("vmess://{0}".format(authority), SRC)
    assert node is None
    assert issue is not None
    assert issue.code == "missing_server"


def test_vmess_missing_id():
    authority = base64.b64encode(
        json.dumps({"add": "198.51.100.11", "port": "443"}).encode("utf-8")
    ).decode("ascii")
    node, issue = parse_uri("vmess://{0}".format(authority), SRC)
    assert node is None
    assert issue is not None
    assert issue.code == "missing_credentials"


def test_vmess_bad_port():
    authority = base64.b64encode(
        json.dumps({"add": "198.51.100.12", "port": "abc", "id": VLESS_UUID}).encode(
            "utf-8"
        )
    ).decode("ascii")
    node, issue = parse_uri("vmess://{0}".format(authority), SRC)
    assert node is None
    assert issue is not None
    assert issue.code == "invalid_port"


def test_vmess_invalid_json_without_authority_falls_back():
    node, issue = parse_uri("vmess://dGVzdA==", SRC)
    assert node is None
    assert issue is not None
    assert issue.code == "unsupported_protocol"


def test_trojan_parses():
    node, issue = parse_uri(
        "trojan://sample-password@198.51.100.7:443?security=tls"
        "&peer=edge.example.test#sample-b",
        SRC,
    )
    assert issue is None
    assert node.protocol == "trojan"
    assert node.server == "198.51.100.7"
    assert node.port == 443
    assert node.transport == "tcp"
    assert node.security == "tls"
    assert node.secrets == {"password": "sample-password"}
    assert node.params == {"sni": "edge.example.test"}
    assert node.remarks == "sample-b"


def test_trojan_missing_password():
    node, issue = parse_uri("trojan://@198.51.100.7:443#r", SRC)
    assert node is None
    assert issue is not None
    assert issue.code == "missing_credentials"


def test_ss_sip002_standard():
    line = "ss://YWVzLTI1Ni1nY206c2FtcGxlLXBhc3N3b3Jk@203.0.113.9:8388#sample-ss"
    node, issue = parse_uri(line, SRC)
    assert issue is None
    assert node.protocol == "ss"
    assert node.server == "203.0.113.9"
    assert node.port == 8388
    assert node.secrets == {"password": "sample-password"}
    assert node.params["method"] == "aes-256-gcm"
    assert node.remarks == "sample-ss"


def test_ss_plaintext_userinfo():
    line = "ss://aes-256-gcm:sample-password@203.0.113.10:8388#r"
    node, issue = parse_uri(line, SRC)
    assert issue is None
    assert node.secrets == {"password": "sample-password"}
    assert node.params["method"] == "aes-256-gcm"


def test_ss_whole_authority_base64():
    authority = base64.b64encode(
        b"aes-256-gcm:sample-password@203.0.113.41:8388"
    ).decode("ascii")
    node, issue = parse_uri("ss://{0}#r".format(authority), SRC)
    assert issue is None
    assert node.server == "203.0.113.41"
    assert node.port == 8388
    assert node.secrets == {"password": "sample-password"}
    assert node.params["method"] == "aes-256-gcm"


def test_ss_undecodable_credentials():
    node, issue = parse_uri("ss://plainpassword!@203.0.113.11:8388#r", SRC)
    assert node is None
    assert issue is not None
    assert issue.code == "missing_credentials"


def test_hysteria2_defaults_and_userinfo():
    node, issue = parse_uri("hy2://sample-password@192.0.2.12:443#r", SRC)
    assert issue is None
    assert node.protocol == "hysteria2"
    assert node.transport == "udp"
    assert node.security == "tls"
    assert node.secrets == {"password": "sample-password"}
    assert issue is None
    assert node.server == "192.0.2.12"


def test_hysteria2_query_auth_moves_to_password():
    node, issue = parse_uri("hy2://192.0.2.13:443?auth=sample-password#r", SRC)
    assert issue is None
    assert node.secrets == {"password": "sample-password"}


def test_hysteria2_missing_credentials():
    node, issue = parse_uri("hy2://192.0.2.14:443#r", SRC)
    assert node is None
    assert issue is not None
    assert issue.code == "missing_credentials"


def test_tuic_parses_credentials():
    line = (
        "tuic://123e4567-e89b-12d3-a456-426614174000:sample-password"
        "@203.0.113.12:443?sni=fast.example.test#r"
    )
    node, issue = parse_uri(line, SRC)
    assert issue is None
    assert node.protocol == "tuic"
    assert node.transport == "udp"
    assert node.security == "tls"
    assert node.secrets == {"uuid": VLESS_UUID, "password": "sample-password"}
    assert node.params == {"sni": "fast.example.test"}


def test_tuic_missing_credentials():
    node, issue = parse_uri("tuic://{0}@203.0.113.12:443#r".format(VLESS_UUID), SRC)
    assert node is None
    assert issue is not None
    assert issue.code == "missing_credentials"


def test_invalid_ports_rejected():
    for port_text in ("abc", "0", "65536", "-1"):
        line = "vless://{0}@192.0.2.15:{1}#r".format(VLESS_UUID, port_text)
        node, issue = parse_uri(line, SRC)
        assert node is None, port_text
        assert issue is not None, port_text
        assert issue.code == "invalid_port", port_text


def test_unsupported_scheme_reported():
    node, issue = parse_uri("socks5://user:pass@192.0.2.16:1080#r", SRC)
    assert node is None
    assert issue is not None
    assert issue.code == "unsupported_protocol"
    assert "socks5" in issue.message_redacted
    assert issue.raw_ref == "<socks5>://…"


def test_missing_scheme_separator():
    node, issue = parse_uri("192.0.2.16:443#r", SRC)
    assert node is None
    assert issue is not None
    assert issue.code == "invalid_uri"
    assert issue.raw_ref == "<unknown>://…"


def test_malformed_scheme_rejected():
    node, issue = parse_uri("ht tp://192.0.2.16:443#r", SRC)
    assert node is None
    assert issue is not None
    assert issue.code == "invalid_uri"


def test_bracketed_ipv6_server():
    line = "vless://123e4567-e89b-12d3-a456-426614174000@[2001:db8::1]:443#r"
    node, issue = parse_uri(line, SRC)
    assert issue is None
    assert node.server == "2001:db8::1"
    assert node.port == 443


def test_bare_ipv6_rejected():
    line = "vless://123e4567-e89b-12d3-a456-426614174000@2001:db8::1:443#r"
    node, issue = parse_uri(line, SRC)
    assert node is None
    assert issue is not None
    assert issue.code == "invalid_uri"


def test_fragment_becomes_remarks():
    node, issue = parse_uri(
        "vless://123e4567-e89b-12d3-a456-426614174000@192.0.2.17:443#sample%09name",
        SRC,
    )
    assert issue is None
    assert node.remarks == "samplename"


def test_query_percent_decoding():
    line = (
        "vless://123e4567-e89b-12d3-a456-426614174000@192.0.2.18:443"
        "?type=ws&path=%2Fws%3Fa%3D1#r"
    )
    node, issue = parse_uri(line, SRC)
    assert issue is None
    assert node.params["path"] == "/ws?a=1"


def test_raw_ref_redacts_host():
    node, issue = parse_uri("vless://{0}@198.51.100.20:abc#r".format(VLESS_UUID), SRC)
    assert node is None
    assert issue is not None
    assert issue.raw_ref == "<vless>://…"
    assert "198.51.100" not in issue.raw_ref
    assert "192.0.2" not in issue.raw_ref


def test_params_exclude_transport_security_keys():
    line = (
        "vless://123e4567-e89b-12d3-a456-426614174000@192.0.2.19:443"
        "?type=ws&net=ws&security=tls&tls=tls#r"
    )
    node, issue = parse_uri(line, SRC)
    assert issue is None
    assert node.transport == "ws"
    assert node.security == "tls"
    for key in ("type", "net", "network", "security", "tls"):
        assert key not in node.params


def test_default_header_values_are_folded():
    line = (
        "vless://123e4567-e89b-12d3-a456-426614174000@192.0.2.20:443"
        "?encryption=none&headerType=none&aid=0#r"
    )
    node, issue = parse_uri(line, SRC)
    assert issue is None
    for key in ("encryption", "headerType", "alter_id"):
        assert key not in node.params


def test_alter_id_from_query():
    line = "vless://123e4567-e89b-12d3-a456-426614174000@192.0.2.21:443?aid=2#r"
    node, issue = parse_uri(line, SRC)
    assert issue is None
    assert node.params["alter_id"] == 2


def test_uppercase_scheme_normalized():
    line = "VLESS://123e4567-e89b-12d3-a456-426614174000@192.0.2.22:443#r"
    node, issue = parse_uri(line, SRC)
    assert issue is None
    assert node.protocol == "vless"


def test_missing_server_reported():
    node, issue = parse_uri("vless://123e4567-e89b-12d3-a456-426614174000@:443#r", SRC)
    assert node is None
    assert issue is not None
    assert issue.code == "missing_server"


def test_hysteria2_obfs_sni_normalized():
    line = (
        "hysteria2://sample-password@192.0.2.30:443"
        "?obfs=salamander&obfs-password=sample-obfs&sni=edge.example.test"
        "&insecure=1#hy2-node"
    )
    node, issue = parse_uri(line, SRC)
    assert issue is None
    assert node.protocol == "hysteria2"
    assert node.params["obfs"] == "salamander"
    assert node.params["sni"] == "edge.example.test"
    assert node.params["insecure"] is True
    assert node.secrets == {
        "password": "sample-password",
        "obfs_password": "sample-obfs",
    }
    assert "obfs-password" not in node.params
    assert "obfs_password" not in node.params


def test_hysteria2_insecure_false_folded():
    line = (
        "hysteria2://sample-password@192.0.2.31:443"
        "?obfs=salamander&allow_insecure=0#r"
    )
    node, issue = parse_uri(line, SRC)
    assert issue is None
    assert node.params == {"obfs": "salamander"}
    assert "insecure" not in node.params


def test_tuic_congestion_and_udp_relay_normalized():
    line = (
        "tuic://123e4567-e89b-12d3-a456-426614174000:sample-password"
        "@203.0.113.12:443?congestion_control=bbr&udp_relay_mode=native"
        "&alpn=h3&sni=fast.example.test&allow_insecure=1#tuic-node"
    )
    node, issue = parse_uri(line, SRC)
    assert issue is None
    assert node.params["congestion_control"] == "bbr"
    assert node.params["udp_relay_mode"] == "native"
    assert node.params["alpn"] == "h3"
    assert node.params["sni"] == "fast.example.test"
    assert node.params["insecure"] is True
    assert node.secrets["uuid"] == VLESS_UUID


def test_tuic_congestion_alias_cc():
    line = (
        "tuic://123e4567-e89b-12d3-a456-426614174000:sample-password"
        "@203.0.113.13:443?cc=cubic&urm=native#r"
    )
    node, issue = parse_uri(line, SRC)
    assert issue is None
    assert node.params["congestion_control"] == "cubic"
    assert node.params["udp_relay_mode"] == "native"


def test_unsupported_reason_is_stable_and_redacted():
    line = "socks5://user:secret-password@192.0.2.40:1080#r"
    first, first_issue = parse_uri(line, SRC)
    second, second_issue = parse_uri(line, SRC)
    assert first is None and second is None
    assert first_issue.code == second_issue.code == "unsupported_protocol"
    assert first_issue.message_redacted == second_issue.message_redacted
    assert first_issue.raw_ref == second_issue.raw_ref == "<socks5>://…"
    assert "secret-password" not in first_issue.message_redacted
    assert "192.0.2.40" not in first_issue.raw_ref
