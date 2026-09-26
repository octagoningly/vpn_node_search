from __future__ import annotations

from nodebench.core.schema import FINGERPRINT_VERSION, ParsedEndpoint, ParsedProxy
from nodebench.normalize.canonical import normalize_endpoint, normalize_proxy

VLESS_UUID = "123e4567-e89b-12d3-a456-426614174000"


def test_proxy_normalized_and_stripped():
    node, issue = normalize_proxy(
        ParsedProxy(
            source_id="src-a",
            protocol=" VLESS ",
            server=" 192.0.2.10 ",
            port=443,
            transport="TCP",
            security="TLS",
            remarks="sample-node",
        ),
        "src-a",
    )
    assert issue is None
    assert node.kind == "proxy_node"
    assert node.protocol == "vless"
    assert node.server == "192.0.2.10"
    assert node.transport == "tcp"
    assert node.security == "tls"
    assert node.fingerprint_version == FINGERPRINT_VERSION
    assert node.item_id == node.fingerprint
    assert node.source_ids == ["src-a"]
    assert node.raw_refs == []


def test_proxy_bool_port_rejected():
    data = {
        "source_id": "src-a",
        "protocol": "vless",
        "server": "192.0.2.10",
        "port": True,
        "transport": "tcp",
        "security": "tls",
    }
    node, issue = normalize_proxy(data, "src-a")
    assert node is None
    assert issue is not None
    assert issue.code == "invalid_port"


def test_proxy_missing_protocol_fields():
    node, issue = normalize_proxy(
        ParsedProxy(
            source_id="src-a",
            protocol="",
            server="192.0.2.10",
            port=443,
            transport="tcp",
            security="tls",
        ),
        "src-a",
    )
    assert node is None
    assert issue is not None
    assert issue.code == "unsupported_protocol"


def test_endpoint_normalized():
    node, issue = normalize_endpoint(
        ParsedEndpoint(
            source_id="src-b",
            address=" 198.51.100.42 ",
            port=2053,
            tls=True,
            params={"datacenter": "sample-dc"},
        ),
        "src-b",
    )
    assert issue is None
    assert node.kind == "edge_endpoint"
    assert node.address == "198.51.100.42"
    assert node.port == 2053
    assert node.tls is True
    assert node.target_host == ""
    assert node.item_id == node.fingerprint
    assert node.source_ids == ["src-b"]
    assert node.params == {"datacenter": "sample-dc"}


def test_endpoint_missing_address():
    node, issue = normalize_endpoint(
        ParsedEndpoint(source_id="src-b", address="", port=443),
        "src-b",
    )
    assert node is None
    assert issue is not None
    assert issue.code == "missing_server"


def test_endpoint_invalid_port():
    node, issue = normalize_endpoint(
        ParsedEndpoint(source_id="src-b", address="192.0.2.40", port=0),
        "src-b",
    )
    assert node is None
    assert issue is not None
    assert issue.code == "invalid_port"
    assert issue.source_id == "src-b"
