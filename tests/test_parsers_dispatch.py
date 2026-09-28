from __future__ import annotations

import base64
from datetime import datetime, timezone
from pathlib import Path

import pytest

from nodebench.core.schema import ParseIssue, RawItem
from nodebench.parsers import dispatch as dispatch_mod
from nodebench.parsers.dispatch import parse_raw_item

PROJECT_ROOT = Path(__file__).resolve().parents[1]

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)

VLESS_ONE = (
    "vless://123e4567-e89b-12d3-a456-426614174000@192.0.2.10:443"
    "?type=tcp&security=tls#sample-a\n"
)
TROJAN_ONE = "trojan://sample-password@198.51.100.7:443#sample-b\n"


def make_item(content_type, payload, source_id="src-1", item_id="item-1"):
    return RawItem(
        source_id=source_id,
        content_type=content_type,
        payload=payload,
        fetched_at=NOW,
        license_tag="unknown",
        source_ref="input/sample",
        item_id=item_id,
    )


def read(name):
    return (PROJECT_ROOT / "input" / name).read_text(encoding="utf-8")


def test_uri_list_content_type():
    item = make_item("uri_list", VLESS_ONE + TROJAN_ONE)
    proxies, endpoints, issues = parse_raw_item(item)
    assert [node.protocol for node in proxies] == ["vless", "trojan"]
    assert endpoints == []
    assert issues == []
    assert proxies[0].source_id == "src-1"


def test_base64_sub_content_type():
    item = make_item("base64_sub", read("sample-base64.txt"))
    proxies, endpoints, issues = parse_raw_item(item)
    assert len(proxies) == 2
    assert proxies[0].protocol == "vless"
    assert proxies[1].protocol == "hysteria2"
    assert issues == []


def test_yaml_content_type():
    item = make_item("yaml", read("sample-clash.yaml"))
    proxies, endpoints, issues = parse_raw_item(item)
    assert len(proxies) == 2
    assert endpoints == []
    assert issues == []


def test_csv_content_type():
    item = make_item("csv", read("sample-endpoints.csv"))
    proxies, endpoints, issues = parse_raw_item(item)
    assert proxies == []
    assert len(endpoints) == 3
    assert issues == []


def test_text_content_type_parses_uri_lines():
    item = make_item("text", VLESS_ONE + "remember to retest this folder later\n")
    proxies, endpoints, issues = parse_raw_item(item)
    assert len(proxies) == 1
    assert issues == []


def test_text_without_uris_reports_unsupported():
    item = make_item("text", "remember to retest this folder later\n")
    proxies, endpoints, issues = parse_raw_item(item)
    assert proxies == []
    assert issues[0].code == "unsupported_content"
    assert issues[0].source_id == "src-1"


def test_uri_list_garbage_line_reported_with_line_ref():
    payload = VLESS_ONE + "garbage without scheme\n" + TROJAN_ONE
    item = make_item("uri_list", payload)
    proxies, endpoints, issues = parse_raw_item(item)
    assert len(proxies) == 2
    assert len(issues) == 1
    assert issues[0].code == "unsupported_content"
    assert issues[0].raw_ref == "line:2"


def test_comments_and_blank_lines_skipped():
    payload = "# sample comment\n\n" + VLESS_ONE + "   \n"
    item = make_item("uri_list", payload)
    proxies, endpoints, issues = parse_raw_item(item)
    assert len(proxies) == 1
    assert issues == []


def test_invalid_base64_payload_reported():
    item = make_item("base64_sub", "not a subscription payload")
    proxies, endpoints, issues = parse_raw_item(item)
    assert proxies == []
    assert issues[0].code == "invalid_base64"


def test_parse_error_is_redacted(monkeypatch):
    def boom(line, source_id):
        raise ValueError("secret-x")

    monkeypatch.setattr(dispatch_mod, "parse_uri", boom)
    item = make_item("uri_list", VLESS_ONE)
    proxies, endpoints, issues = parse_raw_item(item)
    assert proxies == []
    assert endpoints == []
    assert len(issues) == 1
    assert issues[0].code == "parse_error"
    assert issues[0].message_redacted == "ValueError"
    assert "secret-x" not in issues[0].message_redacted
    assert issues[0].source_id == "src-1"


def test_issue_fields_backfilled(monkeypatch):
    def fake_parse_uri(line, source_id):
        issue = ParseIssue(
            source_id="",
            code="invalid_uri",
            message_redacted="boom",
            raw_ref="",
        )
        return None, issue

    monkeypatch.setattr(dispatch_mod, "parse_uri", fake_parse_uri)
    item = make_item("uri_list", VLESS_ONE + TROJAN_ONE)
    proxies, endpoints, issues = parse_raw_item(item)
    assert proxies == []
    assert len(issues) == 2
    assert [issue.raw_ref for issue in issues] == ["line:1", "line:2"]
    assert all(issue.source_id == "src-1" for issue in issues)


def test_unknown_content_type_reported():
    item = RawItem.model_construct(
        source_id="src-9",
        content_type="binary",
        payload="zz",
        fetched_at=NOW,
        license_tag="unknown",
        source_ref="input/blob",
        item_id="item-9",
    )
    proxies, endpoints, issues = parse_raw_item(item)
    assert proxies == []
    assert endpoints == []
    assert issues[0].code == "unsupported_content"
    assert issues[0].source_id == "src-9"


def test_yaml_issue_keeps_its_own_reference():
    item = make_item("yaml", "proxies:\n  - type: quantum\n")
    proxies, endpoints, issues = parse_raw_item(item)
    assert proxies == []
    assert issues[0].code == "unsupported_protocol"
    assert issues[0].raw_ref == "proxies:0"
    assert issues[0].source_id == "src-1"


def test_csv_issue_keeps_line_reference():
    item = make_item("csv", "not_a_host!,443\n")
    proxies, endpoints, issues = parse_raw_item(item)
    assert endpoints == []
    assert issues[0].code == "invalid_row"
    assert issues[0].raw_ref == "line:1"
