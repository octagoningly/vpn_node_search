from __future__ import annotations

import re
from pathlib import Path

import nodebench.sources.cf as cf_mod
from nodebench.core.config import AppConfig, CfSourceConfig, SourcesConfig
from nodebench.core.schema import RunContext
from nodebench.sources.cf import collect_cf

PROJECT_ROOT = Path(__file__).resolve().parents[1]
ENDPOINT_PATTERN = re.compile(r"\d{1,3}(?:\.\d{1,3}){3}:\d+")


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))
    return path


def cf_config(*files: str, enabled: bool = True, limit: int = 200) -> AppConfig:
    return AppConfig(
        sources=SourcesConfig(
            cf=CfSourceConfig(
                enabled=enabled,
                candidate_files=list(files),
                max_ips_per_run=limit,
            )
        )
    )


def test_single_ip_file_produces_one_endpoint_item(tmp_path: Path):
    write(tmp_path / "input" / "cf.txt", "192.0.2.7\n")
    items, reports = collect_cf(cf_config("input/cf.txt"), base_dir=tmp_path)
    assert len(items) == 1
    item = items[0]
    assert item.source_id == "cf"
    assert item.content_type == "text"
    assert item.payload == "192.0.2.7:443"
    assert item.license_tag == "operator-supplied"
    assert item.source_ref == "input/cf.txt"
    report = reports[0]
    assert report.source_id == "cf"
    assert report.ok is True
    assert report.fetched == 1
    assert report.scope == "input/cf.txt"
    assert report.errors == []


def test_cidr_is_sampled_within_limit(tmp_path: Path):
    write(tmp_path / "input" / "cidr.txt", "192.0.2.0/24\n")
    items, reports = collect_cf(
        cf_config("input/cidr.txt", limit=10), base_dir=tmp_path
    )
    lines = items[0].payload.splitlines()
    assert len(lines) == 10
    assert lines[0] == "192.0.2.0:443"
    assert lines[-1] == "192.0.2.230:443"
    assert len(set(lines)) == 10
    report = reports[0]
    assert report.ok is True
    assert [error.code for error in report.errors] == ["ip_limit_exceeded"]


def test_iptest_csv_uses_ip_and_port_columns(tmp_path: Path):
    write(
        tmp_path / "input" / "candidates.csv",
        "IP地址,端口,回源端口,TLS,数据中心,地区,城市,TCP延迟(ms),速度(MB/s)\n"
        "192.0.2.81,443,80,true,香港,HK,Hong Kong,15.6,7.82\n"
        "198.51.100.42,2053,80,true,东京,JP,Tokyo,21.4,6.35\n",
    )
    items, reports = collect_cf(cf_config("input/candidates.csv"), base_dir=tmp_path)
    assert items[0].content_type == "csv"
    assert items[0].payload == "192.0.2.81:443\n198.51.100.42:2053"
    assert "7.82" not in items[0].payload
    assert "6.35" not in items[0].payload
    assert reports[0].ok is True
    assert reports[0].errors == []


def test_speedtest_csv_without_port_column_defaults_to_443(tmp_path: Path):
    write(
        tmp_path / "input" / "speed.csv",
        "IP 地址,已发送,已接收,丢包率,TCP延迟,下载速度,地区\n"
        "192.0.2.90,4,4,0.00,12.34,6.78,HK\n",
    )
    items, reports = collect_cf(cf_config("input/speed.csv"), base_dir=tmp_path)
    assert items[0].content_type == "csv"
    assert items[0].payload == "192.0.2.90:443"
    assert reports[0].ok is True


def test_disabled_source_returns_nothing(tmp_path: Path):
    write(tmp_path / "input" / "cf.txt", "192.0.2.7\n")
    items, reports = collect_cf(
        cf_config("input/cf.txt", enabled=False), base_dir=tmp_path
    )
    assert items == []
    assert reports == []


def test_missing_file_error_hides_absolute_path(tmp_path: Path):
    config = cf_config(str(tmp_path / "absent.txt"))
    items, reports = collect_cf(config, base_dir=tmp_path)
    assert items == []
    report = reports[0]
    assert report.ok is False
    assert report.errors[0].code == "file_missing"
    assert "absent.txt" in report.errors[0].message_redacted
    assert str(tmp_path) not in report.errors[0].message_redacted
    assert str(tmp_path) not in report.model_dump_json()
    assert report.scope == "absent.txt"


def test_file_outside_base_uses_basename_only(tmp_path: Path):
    outside = write(tmp_path / "outside" / "edge.txt", "192.0.2.50\n")
    config = cf_config(str(outside))
    items, reports = collect_cf(config, base_dir=tmp_path / "project")
    assert items[0].payload == "192.0.2.50:443"
    assert items[0].source_ref == "edge.txt"
    report = reports[0]
    assert report.scope == "edge.txt"
    assert str(tmp_path) not in report.model_dump_json()
    assert str(tmp_path) not in items[0].model_dump_json()


def test_oversized_file_is_reported(monkeypatch, tmp_path: Path):
    write(tmp_path / "input" / "big.txt", "192.0.2.1\n192.0.2.2\n")
    monkeypatch.setattr(cf_mod, "MAX_CF_FILE_BYTES", 16)
    items, reports = collect_cf(cf_config("input/big.txt"), base_dir=tmp_path)
    assert items == []
    report = reports[0]
    assert report.ok is False
    assert report.errors[0].code == "file_too_large"


def test_inline_ports_and_comments(tmp_path: Path):
    write(
        tmp_path / "input" / "mixed.txt",
        "192.0.2.1 # edge-a\n198.51.100.9:2053\n\n# skip\n203.0.113.4,8443\n",
    )
    items, reports = collect_cf(cf_config("input/mixed.txt"), base_dir=tmp_path)
    assert items[0].payload == "192.0.2.1:443\n198.51.100.9:2053\n203.0.113.4:8443"
    assert reports[0].ok is True


def test_empty_file_is_reported(tmp_path: Path):
    write(tmp_path / "input" / "empty.txt", "   \n\n")
    items, reports = collect_cf(cf_config("input/empty.txt"), base_dir=tmp_path)
    assert items == []
    report = reports[0]
    assert report.ok is False
    assert report.errors[0].code == "empty_file"


def test_enabled_without_files_reports_no_files(tmp_path: Path):
    items, reports = collect_cf(cf_config(), base_dir=tmp_path)
    assert items == []
    report = reports[0]
    assert report.ok is False
    assert report.errors[0].code == "no_files"
    assert report.scope == ""


def test_run_context_accepted(tmp_path: Path):
    write(tmp_path / "input" / "cf.txt", "192.0.2.7\n")
    ctx = RunContext.create(profile="local", runner_id="local:desktop-a")
    items, reports = collect_cf(cf_config("input/cf.txt"), ctx, base_dir=tmp_path)
    assert items[0].payload == "192.0.2.7:443"
    assert reports[0].ok is True


def test_path_context_supplies_base(tmp_path: Path):
    write(tmp_path / "input" / "cf.txt", "192.0.2.7\n")
    items, _ = collect_cf(cf_config("input/cf.txt"), tmp_path)
    assert items[0].payload == "192.0.2.7:443"


def test_duplicate_endpoints_are_deduplicated(tmp_path: Path):
    write(tmp_path / "input" / "a.txt", "192.0.2.1\n")
    write(tmp_path / "input" / "b.txt", "192.0.2.1\n192.0.2.2\n")
    items, reports = collect_cf(
        cf_config("input/a.txt", "input/b.txt"), base_dir=tmp_path
    )
    assert [item.payload for item in items] == ["192.0.2.1:443", "192.0.2.2:443"]
    assert reports[0].fetched == 2
    assert reports[0].errors == []


def test_budget_is_shared_across_candidate_files(tmp_path: Path):
    write(
        tmp_path / "input" / "a.txt",
        "192.0.2.1\n192.0.2.2\n192.0.2.3\n",
    )
    write(tmp_path / "input" / "b.txt", "198.51.100.1\n198.51.100.2\n")
    items, reports = collect_cf(
        cf_config("input/a.txt", "input/b.txt", limit=4), base_dir=tmp_path
    )
    assert items[0].payload == "192.0.2.1:443\n192.0.2.2:443\n192.0.2.3:443"
    assert items[1].payload == "198.51.100.1:443"
    report = reports[0]
    assert report.ok is True
    assert report.fetched == 2
    assert [error.code for error in report.errors] == ["ip_limit_exceeded"]
    assert report.scope == "input/a.txt, input/b.txt"


def test_project_candidate_samples_are_importable():
    config = cf_config("input/cf-candidates.txt", "input/cf-candidates.csv")
    items, reports = collect_cf(config, base_dir=PROJECT_ROOT)
    assert [item.content_type for item in items] == ["text", "csv"]
    report = reports[0]
    assert report.ok is True
    assert report.errors == []
    lines = [line for item in items for line in item.payload.splitlines()]
    assert len(lines) == 13
    assert all(ENDPOINT_PATTERN.fullmatch(line) for line in lines)
    assert "7.82" not in items[1].payload
    assert "MB/s" not in items[1].payload
