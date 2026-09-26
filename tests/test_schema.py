from __future__ import annotations

import re

import pytest
from pydantic import ValidationError

from nodebench.core.schema import (
    FINGERPRINT_VERSION,
    RUN_ID_PATTERN,
    SCHEMA_VERSION,
    EdgeEndpoint,
    ErrorInfo,
    Kind,
    ProxyNode,
    RunContext,
    Status,
)


def make_proxy_node() -> ProxyNode:
    return ProxyNode(
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


def test_status_enum_is_complete():
    assert [status.value for status in Status] == [
        "ok",
        "failed",
        "skipped",
        "unsupported",
        "unknown",
        "stale",
    ]


def test_kind_enum_is_complete():
    assert [kind.value for kind in Kind] == ["proxy_node", "edge_endpoint"]


def test_schema_versions_are_1():
    assert SCHEMA_VERSION == 1
    assert FINGERPRINT_VERSION == 1


def test_error_info_rejects_extra_fields():
    with pytest.raises(ValidationError):
        ErrorInfo.model_validate(
            {
                "stage": "collect",
                "code": "source_failed",
                "message_redacted": "boom",
                "unexpected": 1,
            }
        )


def test_proxy_node_rejects_extra_fields():
    payload = make_proxy_node().model_dump()
    payload["surprise"] = True
    with pytest.raises(ValidationError):
        ProxyNode.model_validate(payload)


def test_edge_endpoint_rejects_extra_fields():
    with pytest.raises(ValidationError):
        EdgeEndpoint.model_validate(
            {
                "item_id": "hmac-sha256:example",
                "kind": "edge_endpoint",
                "fingerprint": "fingerprint-example",
                "fingerprint_version": 1,
                "address": "198.51.100.7",
                "port": 443,
                "surprise": True,
            }
        )


def test_run_id_format():
    context = RunContext.create(profile="local", runner_id="local:desktop-a")
    assert re.fullmatch(RUN_ID_PATTERN, context.run_id)


def test_run_context_rejects_bad_run_id():
    with pytest.raises(ValidationError):
        RunContext(run_id="not-a-run-id", runner_id="local:desktop-a", profile="local")


def test_default_factories_are_independent():
    first = make_proxy_node()
    second = make_proxy_node()
    first.source_ids.append("local-1")
    first.params["region"] = "jp"
    assert second.source_ids == []
    assert second.params == {}


def test_secrets_field_is_hidden_from_repr():
    node = make_proxy_node()
    node.secrets["uuid"] = "123e4567-e89b-12d3-a456-426614174000"
    assert "123e4567-e89b-12d3-a456-426614174000" not in repr(node)
