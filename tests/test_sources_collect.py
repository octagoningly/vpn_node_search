from __future__ import annotations

from pathlib import Path

from nodebench.core.config import (
    AppConfig,
    GithubSourceConfig,
    LocalSourceConfig,
    SourcesConfig,
    SubscriptionSourceConfig,
)
from nodebench.sources import collect as collect_mod
from nodebench.sources.collect import collect_all

URI_TEXT = (
    "vless://123e4567-e89b-12d3-a456-426614174000@192.0.2.10:443?type=tcp#sample-a\n"
)


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))
    return path


def local_config() -> AppConfig:
    return AppConfig(
        sources=SourcesConfig(local=LocalSourceConfig(enabled=True, paths=["input"]))
    )


def test_collect_all_runs_only_local(tmp_path: Path):
    write(tmp_path / "input" / "nodes.txt", URI_TEXT)
    outcome = collect_all(local_config(), base_dir=tmp_path)
    assert len(outcome.items) == 1
    assert len(outcome.reports) == 1
    report = outcome.reports[0]
    assert report.source_id == "local"
    assert report.ok is True
    assert report.fetched == 1


def test_all_sources_disabled_returns_empty(tmp_path: Path):
    write(tmp_path / "input" / "nodes.txt", URI_TEXT)
    outcome = collect_all(AppConfig(), base_dir=tmp_path)
    assert outcome.items == []
    assert outcome.reports == []


def test_github_adapter_runs_without_token(tmp_path: Path):
    write(tmp_path / "input" / "nodes.txt", URI_TEXT)
    config = local_config()
    config.sources.github = GithubSourceConfig(enabled=True, offline=True)
    outcome = collect_all(config, base_dir=tmp_path)
    assert len(outcome.items) == 1
    by_id = {report.source_id: report for report in outcome.reports}
    assert by_id["local"].ok is True
    assert by_id["github"].mode == "offline"
    codes = {error.code for error in by_id["github"].errors}
    assert "offline_mode" in codes or "missing_token" in codes or not by_id["github"].errors


def test_adapter_exception_becomes_failed_report(monkeypatch, tmp_path: Path):
    def boom(*args, **kwargs):
        raise RuntimeError(
            "collect exploded for uuid 123e4567-e89b-12d3-a456-426614174000"
        )

    monkeypatch.setattr(collect_mod, "collect_local", boom)
    outcome = collect_all(local_config(), base_dir=tmp_path)
    assert outcome.items == []
    report = outcome.reports[0]
    assert report.source_id == "local"
    assert report.ok is False
    assert report.errors[0].code == "source_error"
    message = report.errors[0].message_redacted
    assert "RuntimeError" in message
    assert "123e4567-e89b-12d3-a456-426614174000" not in message
    assert "***" in message


def test_subscription_report_never_echoes_urls(tmp_path: Path):
    config = local_config()
    config.sources.subscriptions = SubscriptionSourceConfig(
        enabled=True,
        urls=["https://subs.example.test/list?token=abc12345"],
        offline=True,
    )
    outcome = collect_all(config, base_dir=tmp_path)
    report = next(
        item for item in outcome.reports if item.source_id == "subscriptions"
    )
    text = report.model_dump_json()
    assert "subs.example.test" not in text
    assert "abc12345" not in text
