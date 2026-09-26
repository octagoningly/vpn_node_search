from __future__ import annotations

from pathlib import Path

from nodebench.core.config import (
    AppConfig,
    CfSourceConfig,
    LocalSourceConfig,
    SourcesConfig,
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


def mixed_config(cf_files: list[str], limit: int = 200) -> AppConfig:
    config = local_config()
    config.sources.cf = CfSourceConfig(
        enabled=True, candidate_files=cf_files, max_ips_per_run=limit
    )
    return config


def test_cf_error_does_not_block_local(tmp_path: Path):
    write(tmp_path / "input" / "nodes.txt", URI_TEXT)
    config = mixed_config(["cf/missing.txt"])
    outcome = collect_all(config, base_dir=tmp_path)
    by_id = {report.source_id: report for report in outcome.reports}
    assert set(by_id) == {"local", "cf"}
    assert by_id["local"].ok is True
    assert by_id["local"].fetched == 1
    assert by_id["cf"].ok is False
    assert by_id["cf"].errors[0].code == "file_missing"
    assert len(outcome.items) == 1
    assert outcome.items[0].source_id == "local"


def test_cf_adapter_exception_is_isolated(monkeypatch, tmp_path: Path):
    write(tmp_path / "input" / "nodes.txt", URI_TEXT)

    def boom(*args, **kwargs):
        raise RuntimeError("cf collect exploded for 192.0.2.7")

    monkeypatch.setattr(collect_mod, "collect_cf", boom)
    config = mixed_config(["cf/candidates.txt"])
    outcome = collect_all(config, base_dir=tmp_path)
    by_id = {report.source_id: report for report in outcome.reports}
    assert by_id["local"].ok is True
    assert by_id["cf"].ok is False
    assert by_id["cf"].errors[0].code == "source_error"
    assert "RuntimeError" in by_id["cf"].errors[0].message_redacted
    assert len(outcome.items) == 1


def test_collect_all_runs_cf_and_local(tmp_path: Path):
    write(tmp_path / "input" / "nodes.txt", URI_TEXT)
    write(tmp_path / "cf" / "candidates.txt", "192.0.2.7\n")
    config = mixed_config(["cf/candidates.txt"])
    outcome = collect_all(config, base_dir=tmp_path)
    by_id = {report.source_id: report for report in outcome.reports}
    assert by_id["local"].ok is True
    assert by_id["cf"].ok is True
    assert {item.source_id for item in outcome.items} == {"local", "cf"}
    cf_item = next(item for item in outcome.items if item.source_id == "cf")
    assert cf_item.payload == "192.0.2.7:443"


def test_cf_budget_does_not_reduce_local_output(tmp_path: Path):
    write(tmp_path / "input" / "nodes.txt", URI_TEXT)
    write(tmp_path / "cf" / "cidr.txt", "192.0.2.0/28\n")
    config = mixed_config(["cf/cidr.txt"], limit=3)
    outcome = collect_all(config, base_dir=tmp_path)
    by_id = {report.source_id: report for report in outcome.reports}
    assert by_id["local"].fetched == 1
    cf_item = next(item for item in outcome.items if item.source_id == "cf")
    assert len(cf_item.payload.splitlines()) == 3
    assert by_id["cf"].ok is True
    assert [error.code for error in by_id["cf"].errors] == ["ip_limit_exceeded"]


def test_collect_all_accepts_path_as_context(tmp_path: Path):
    write(tmp_path / "cf" / "candidates.txt", "192.0.2.7\n")
    config = AppConfig(
        sources=SourcesConfig(
            cf=CfSourceConfig(enabled=True, candidate_files=["cf/candidates.txt"])
        )
    )
    outcome = collect_all(config, tmp_path)
    assert [report.source_id for report in outcome.reports] == ["cf"]
    assert outcome.reports[0].ok is True
    assert outcome.items[0].payload == "192.0.2.7:443"


def test_collect_all_accepts_string_context(tmp_path: Path):
    write(tmp_path / "cf" / "candidates.txt", "192.0.2.7\n")
    config = AppConfig(
        sources=SourcesConfig(
            cf=CfSourceConfig(enabled=True, candidate_files=["cf/candidates.txt"])
        )
    )
    outcome = collect_all(config, str(tmp_path))
    assert outcome.reports[0].ok is True
    assert outcome.items[0].payload == "192.0.2.7:443"
