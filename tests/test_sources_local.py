from __future__ import annotations

import base64
from pathlib import Path

from nodebench.core.config import LocalSourceConfig
from nodebench.sources import base as base_mod
from nodebench.sources import local as local_mod
from nodebench.sources.local import collect_local

PROJECT_ROOT = Path(__file__).resolve().parents[1]

URI_TEXT = (
    "vless://123e4567-e89b-12d3-a456-426614174000@192.0.2.10:443?type=tcp#sample-a\n"
    "trojan://sample-password@198.51.100.7:443#sample-b\n"
)


def source_for(*paths: str) -> LocalSourceConfig:
    return LocalSourceConfig(enabled=True, paths=list(paths))


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))
    return path


def test_uri_list_item_fields(tmp_path: Path):
    write(tmp_path / "input" / "nodes.txt", URI_TEXT)
    outcome = collect_local(source_for("input"), base_dir=tmp_path)
    assert len(outcome.items) == 1
    item = outcome.items[0]
    assert item.source_id == "local"
    assert item.content_type == "uri_list"
    assert item.payload == URI_TEXT
    assert item.license_tag == "unknown"
    assert item.source_ref == "input/nodes.txt"
    assert item.item_id is None
    assert item.fetched_at.tzinfo is not None
    report = outcome.reports[0]
    assert report.ok is True
    assert report.fetched == 1
    assert report.errors == []
    assert report.scope == "input"


def test_base64_subscription_detected_by_sniffing(tmp_path: Path):
    encoded = base64.b64encode(URI_TEXT.encode("utf-8")).decode("ascii")
    write(tmp_path / "input" / "sub.txt", encoded)
    outcome = collect_local(source_for("input"), base_dir=tmp_path)
    assert outcome.items[0].content_type == "base64_sub"


def test_extension_takes_priority_over_sniffing(tmp_path: Path):
    write(tmp_path / "input" / "literal.b64", URI_TEXT)
    write(
        tmp_path / "input" / "with-url.yaml",
        "proxies: []\nurl: https://example.test/docs\n",
    )
    write(tmp_path / "input" / "endpoints.csv", "ip,port\n192.0.2.40,443\n")
    outcome = collect_local(source_for("input"), base_dir=tmp_path)
    types = {item.source_ref: item.content_type for item in outcome.items}
    assert types["input/literal.b64"] == "base64_sub"
    assert types["input/with-url.yaml"] == "yaml"
    assert types["input/endpoints.csv"] == "csv"


def test_plain_notes_are_text(tmp_path: Path):
    write(tmp_path / "input" / "notes.txt", "remember to retest this folder later\n")
    outcome = collect_local(source_for("input"), base_dir=tmp_path)
    assert outcome.items[0].content_type == "text"


def test_disabled_source_returns_empty(tmp_path: Path):
    write(tmp_path / "input" / "nodes.txt", URI_TEXT)
    source = LocalSourceConfig(enabled=False, paths=["input"])
    outcome = collect_local(source, base_dir=tmp_path)
    assert outcome.items == []
    assert outcome.reports == []


def test_missing_path_fails_the_source(tmp_path: Path):
    outcome = collect_local(source_for("missing-dir"), base_dir=tmp_path)
    report = outcome.reports[0]
    assert report.ok is False
    assert report.fetched == 0
    assert report.errors[0].code == "path_missing"
    assert "missing-dir" in report.errors[0].message_redacted


def test_no_paths_configured(tmp_path: Path):
    outcome = collect_local(source_for(), base_dir=tmp_path)
    report = outcome.reports[0]
    assert report.ok is False
    assert report.errors[0].code == "no_paths"


def test_file_size_limit(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(local_mod, "MAX_SOURCE_FILE_BYTES", 16)
    write(tmp_path / "input" / "big.txt", "x" * 17)
    outcome = collect_local(source_for("input"), base_dir=tmp_path)
    report = outcome.reports[0]
    assert report.ok is False
    assert report.errors[0].code == "file_too_large"
    assert "bytes" in report.errors[0].message_redacted


def test_invalid_utf8_file_is_reported(tmp_path: Path):
    path = tmp_path / "input" / "binary.bin"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\xff\xfe\x00broken")
    outcome = collect_local(source_for("input"), base_dir=tmp_path)
    report = outcome.reports[0]
    assert report.ok is False
    assert report.errors[0].code == "decode_failed"


def test_empty_file_is_skipped_with_error(tmp_path: Path):
    write(tmp_path / "input" / "empty.txt", "\n  \n")
    outcome = collect_local(source_for("input"), base_dir=tmp_path)
    report = outcome.reports[0]
    assert report.ok is False
    assert report.fetched == 0
    assert report.errors[0].code == "empty_file"


def test_directory_recursion_skips_hidden_entries(tmp_path: Path):
    write(tmp_path / "input" / "a.txt", URI_TEXT)
    write(tmp_path / "input" / "nested" / "b.txt", URI_TEXT)
    write(tmp_path / "input" / ".hidden.txt", URI_TEXT)
    write(tmp_path / "input" / ".git" / "c.txt", URI_TEXT)
    outcome = collect_local(source_for("input"), base_dir=tmp_path)
    refs = sorted(item.source_ref for item in outcome.items)
    assert refs == ["input/a.txt", "input/nested/b.txt"]


def test_file_limit_is_enforced(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(local_mod, "MAX_SOURCE_FILES", 2)
    write(tmp_path / "input" / "a.txt", URI_TEXT)
    write(tmp_path / "input" / "b.txt", URI_TEXT)
    write(tmp_path / "input" / "c.txt", URI_TEXT)
    outcome = collect_local(source_for("input"), base_dir=tmp_path)
    report = outcome.reports[0]
    assert report.fetched == 2
    assert [err.code for err in report.errors] == ["file_limit_exceeded"]


def test_duplicate_paths_yield_single_item(tmp_path: Path):
    write(tmp_path / "input" / "a.txt", URI_TEXT)
    outcome = collect_local(
        source_for("input", "input/a.txt"), base_dir=tmp_path
    )
    assert outcome.reports[0].fetched == 1


def test_byte_order_mark_is_stripped(tmp_path: Path):
    write(tmp_path / "input" / "bom.txt", "\ufeff" + URI_TEXT)
    outcome = collect_local(source_for("input"), base_dir=tmp_path)
    item = outcome.items[0]
    assert item.payload == URI_TEXT
    assert item.content_type == "uri_list"


def test_project_input_samples_collect_offline():
    source = LocalSourceConfig(enabled=True, paths=["input/"])
    outcome = collect_local(source, base_dir=PROJECT_ROOT)
    report = outcome.reports[0]
    assert report.ok is True
    assert report.fetched >= 4
    types = {item.content_type for item in outcome.items}
    assert {"uri_list", "base64_sub", "yaml", "csv"} <= types
    assert all(item.source_ref.startswith("input/") for item in outcome.items)


def test_base64_guard_rejects_non_subscription_text():
    assert base_mod.looks_like_base64("remember to retest this folder") is False
    assert base_mod.looks_like_base64("TW9yZSB0ZXN0IG1vcmUgb2YgdGhpcyB0ZXh0") is False
