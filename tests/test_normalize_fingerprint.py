from __future__ import annotations

from nodebench.core.schema import ParsedEndpoint, ParsedProxy
from nodebench.normalize.fingerprint import (
    DEFAULT_FINGERPRINT_KEY,
    fingerprint_endpoint,
    fingerprint_proxy,
    make_item_id,
)

VLESS_UUID = "123e4567-e89b-12d3-a456-426614174000"


def proxy(**overrides):
    base = {
        "source_id": "src-a",
        "protocol": "vless",
        "server": "192.0.2.10",
        "port": 443,
        "transport": "tcp",
        "security": "tls",
        "params": {"host": "node.example.test"},
        "secrets": {"uuid": VLESS_UUID},
        "remarks": "sample-node",
    }
    base.update(overrides)
    return ParsedProxy(**base)


def endpoint(**overrides):
    base = {
        "source_id": "src-a",
        "address": "192.0.2.40",
        "port": 443,
        "target_host": "",
        "tls": True,
        "params": {"datacenter": "sample-dc"},
        "remarks": "sample-edge",
    }
    base.update(overrides)
    return ParsedEndpoint(**base)


def test_same_inputs_same_fingerprint():
    assert fingerprint_proxy(proxy()) == fingerprint_proxy(proxy())
    assert fingerprint_proxy(proxy()) != ""


def test_fingerprint_has_hmac_prefix():
    value = fingerprint_proxy(proxy())
    assert value.startswith("hmac-sha256:")
    assert len(value) == len("hmac-sha256:") + 64


def test_remarks_and_provenance_do_not_affect_proxy_fingerprint():
    first = proxy()
    second = proxy().model_copy(
        update={
            "remarks": "another-name",
            "source_ids": ["src-b"],
            "raw_refs": ["line:1"],
        }
    )
    assert fingerprint_proxy(first) == fingerprint_proxy(second)


def test_material_fields_change_proxy_fingerprint():
    baseline = fingerprint_proxy(proxy())
    assert fingerprint_proxy(proxy(server="198.51.100.10")) != baseline
    assert fingerprint_proxy(proxy(port=8443)) != baseline
    assert fingerprint_proxy(proxy(transport="ws")) != baseline
    assert fingerprint_proxy(proxy(security="reality")) != baseline
    assert fingerprint_proxy(proxy(protocol="trojan")) != baseline


def test_params_and_secrets_change_proxy_fingerprint():
    baseline = fingerprint_proxy(proxy())
    assert fingerprint_proxy(proxy(params={"host": "other.example.test"})) != baseline
    assert fingerprint_proxy(proxy(secrets={"uuid": "other"})) != baseline


def test_endpoint_fingerprint_ignores_params_and_remarks():
    first = endpoint()
    second = endpoint().model_copy(
        update={"params": {}, "remarks": "another-edge", "source_ids": ["src-b"]}
    )
    assert fingerprint_endpoint(first) == fingerprint_endpoint(second)


def test_endpoint_tls_changes_fingerprint():
    baseline = fingerprint_endpoint(endpoint())
    assert fingerprint_endpoint(endpoint(tls=False)) != baseline
    assert fingerprint_endpoint(endpoint(address="198.51.100.42")) != baseline


def test_proxy_and_endpoint_domains_differ():
    assert fingerprint_proxy(proxy()) != fingerprint_endpoint(endpoint())


def test_make_item_id_is_identity():
    value = fingerprint_proxy(proxy())
    assert make_item_id(value) == value


def test_dict_and_model_inputs_agree():
    data = {
        "protocol": "vless",
        "server": "192.0.2.10",
        "port": 443,
        "transport": "tcp",
        "security": "tls",
        "params": {"host": "node.example.test"},
        "secrets": {"uuid": VLESS_UUID},
    }
    assert fingerprint_proxy(data) == fingerprint_proxy(proxy())


def test_port_as_string_agrees_with_int():
    assert fingerprint_proxy(proxy(port="443")) == fingerprint_proxy(proxy(port=443))


def test_environment_key_changes_fingerprint(monkeypatch):
    monkeypatch.delenv("NODEBENCH_FINGERPRINT_KEY", raising=False)
    baseline = fingerprint_proxy(proxy())
    assert baseline == fingerprint_proxy(proxy())
    assert DEFAULT_FINGERPRINT_KEY == "nodebench-fingerprint-v1"
    monkeypatch.setenv("NODEBENCH_FINGERPRINT_KEY", "nodebench-other-key")
    assert fingerprint_proxy(proxy()) != baseline
    monkeypatch.setenv("NODEBENCH_FINGERPRINT_KEY", "nodebench-fingerprint-v1")
    assert fingerprint_proxy(proxy()) == baseline
