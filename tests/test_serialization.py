from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from nodebench.core.schema import ProxyNode, RawItem
from nodebench.core.serialization import dumps_json, public_dump, write_json_atomic


def make_raw_item() -> RawItem:
    return RawItem(
        source_id="local-1",
        content_type="uri_list",
        payload="vless://123e4567-e89b-12d3-a456-426614174000@192.0.2.10:443",
        fetched_at=datetime(2026, 9, 26, 3, 37, tzinfo=timezone.utc),
        license_tag="example-0",
    )


def make_proxy_node() -> ProxyNode:
    node = ProxyNode(
        item_id="hmac-sha256:example",
        kind="proxy_node",
        fingerprint="fingerprint-example",
        fingerprint_version=1,
        protocol="vless",
        server="192.0.2.10",
        port=443,
        transport="tcp",
        security="tls",
    )
    node.secrets["uuid"] = "123e4567-e89b-12d3-a456-426614174000"
    return node


def test_public_dump_strips_payload_and_secrets():
    dumped = public_dump(make_raw_item())
    assert "payload" not in dumped
    assert "123e4567-e89b-12d3-a456-426614174000" not in dumps_json(dumped)


def test_public_dump_strips_nested_credential_keys():
    dumped = public_dump(
        {
            "ok": True,
            "secrets": {"uuid": "value"},
            "token": "abc",
            "nested": {"password": "p", "keep": "v"},
            "allow_proxy_credentials": False,
        }
    )
    assert dumped["ok"] is True
    assert "secrets" not in dumped
    assert "token" not in dumped
    assert dumped["nested"] == {"keep": "v"}
    assert dumped["allow_proxy_credentials"] is False


def test_public_dump_strips_proxy_node_secrets():
    dumped = public_dump(make_proxy_node())
    assert "secrets" not in dumped
    assert "123e4567-e89b-12d3-a456-426614174000" not in json.dumps(dumped)


def test_public_dump_rejects_non_mapping_input():
    with pytest.raises(TypeError):
        public_dump([1, 2, 3])


def test_dumps_json_datetime_uses_z_suffix():
    text = dumps_json(
        {
            "observed_at": datetime(2026, 9, 26, 3, 37, 0, tzinfo=timezone.utc),
            "naive": datetime(2026, 9, 26, 11, 37, 0),
        }
    )
    assert '"observed_at": "2026-09-26T03:37:00Z"' in text
    assert '"naive": "2026-09-26T11:37:00Z"' in text


def test_dumps_json_keeps_null_as_null():
    text = dumps_json({"exit": {"ip": "203.0.113.7", "asn": None}, "missing": None})
    assert '"asn": null' in text
    assert '"missing": null' in text
    assert '"asn": 0' not in text
    assert '"missing": 0' not in text


def test_dumps_json_is_indented_and_keeps_non_ascii():
    text = dumps_json({"note": "东京-192.0.2.10", "n": 1})
    assert "\n  " in text
    assert "东京" in text


def test_dumps_json_rejects_nan():
    with pytest.raises(ValueError):
        dumps_json({"bad": float("nan")})


def test_write_json_atomic_creates_file_and_leaves_no_temp(tmp_path: Path):
    target = tmp_path / "nested" / "report.json"
    write_json_atomic(target, {"schema_version": 1})
    assert json.loads(target.read_text(encoding="utf-8")) == {"schema_version": 1}
    assert [path.name for path in target.parent.iterdir()] == ["report.json"]


def test_write_json_atomic_overwrites_existing_file(tmp_path: Path):
    target = tmp_path / "report.json"
    write_json_atomic(target, {"round": 1})
    write_json_atomic(target, {"round": 2})
    assert json.loads(target.read_text(encoding="utf-8")) == {"round": 2}
    assert [path.name for path in tmp_path.iterdir()] == ["report.json"]
