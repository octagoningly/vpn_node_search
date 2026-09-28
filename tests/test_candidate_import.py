"""Dual-runtime candidate import: ADDAPI / ADDCSV lists merge into CF candidates."""

from __future__ import annotations

from pathlib import Path

from nodebench.core.config import (
    AppConfig,
    CandidateImportConfig,
    CfSourceConfig,
    SourcesConfig,
)
from nodebench.core.orchestrator import run_pipeline
from nodebench.core.schema import RunContext
from nodebench.normalize import dedupe, normalize_all
from nodebench.parsers import parse_raw_item
from nodebench.sources.candidate_import import (
    SOURCE_ID,
    collect_candidate_import,
    detect_import_format,
)
from nodebench.sources.cf import collect_cf

ADDAPI_TEXT = (
    "150.230.206.130:443#JP\n"
    "151.145.67.90:8443#JP\n"
    "[2606:4700:3037:e1:a64b:6580:941f:e09]:80#官方优选IPv6\n"
)

ADDCSV_TEXT = (
    "IP地址,端口,回源端口,TLS,数据中心,地区,城市,TCP延迟(ms),速度(MB/s)\n"
    "130.61.203.115,8443,443,true,FRA,Europe,Frankfurt,195,11.60\n"
    "138.3.220.4,2082,2082,false,NRT,Asia Pacific,Tokyo,73,11.59\n"
)


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))
    return path


def import_config(*files: str, license_tag: str = "user_supplied") -> AppConfig:
    return AppConfig(
        sources=SourcesConfig(
            candidate_import=CandidateImportConfig(
                enabled=True,
                files=list(files),
                license_tag=license_tag,
            )
        )
    )


def test_detect_import_format():
    assert detect_import_format(ADDCSV_TEXT, Path("x.csv")) == "csv"
    assert detect_import_format(ADDAPI_TEXT, Path("x.txt")) == "endpoint_list"
    assert detect_import_format(ADDAPI_TEXT, Path("x.csv")) == "endpoint_list"


def test_import_addapi_items_use_imported_source_id(tmp_path: Path):
    path = write(tmp_path / "lists" / "addapi.txt", ADDAPI_TEXT)
    items, reports = collect_candidate_import(
        import_config("lists/addapi.txt"), base_dir=tmp_path
    )
    assert reports[0].source_id == SOURCE_ID
    assert reports[0].ok is True
    assert reports[0].fetched == 1
    item = items[0]
    assert item.source_id == "imported"
    assert item.content_type == "endpoint_list"
    assert item.license_tag == "user_supplied"
    assert "150.230.206.130:443#JP" in item.payload
    assert item.source_ref == "lists/addapi.txt"


def test_import_addcsv_keeps_full_rows_for_history(tmp_path: Path):
    path = write(tmp_path / "lists" / "addcsv.csv", ADDCSV_TEXT)
    items, reports = collect_candidate_import(
        import_config("lists/addcsv.csv"), base_dir=tmp_path
    )
    assert reports[0].ok is True
    item = items[0]
    assert item.source_id == "imported"
    assert item.content_type == "csv"
    assert "130.61.203.115,8443,443,true" in item.payload
    assert "11.60" in item.payload


def test_imported_history_lands_in_params_not_measurements(tmp_path: Path):
    write(tmp_path / "lists" / "addcsv.csv", ADDCSV_TEXT)
    items, _reports = collect_candidate_import(
        import_config("lists/addcsv.csv"), base_dir=tmp_path
    )
    proxies, endpoints, issues = parse_raw_item(items[0])
    assert proxies == []
    assert issues == []
    assert len(endpoints) == 2
    first = endpoints[0]
    assert first.source_id == "imported"
    assert first.params["historical_latency_ms"] == 195.0
    assert first.params["historical_speed_mb_s"] == 11.6
    assert first.params["origin_port"] == 443
    dumped = first.model_dump()
    assert "latency_ms" not in dumped
    assert "speed_mb_s" not in dumped

    nodes, edges, _ = normalize_all(proxies, endpoints, issues)
    assert len(edges) == 1 or len(edges) == 2
    edge = edges[0]
    assert "historical_latency_ms" in edge.params
    # EdgeEndpoint is not a measurement record either
    assert not hasattr(edge, "latency_ms")


def test_imported_addapi_remarks_are_historical_reference(tmp_path: Path):
    write(tmp_path / "lists" / "addapi.txt", ADDAPI_TEXT)
    items, _ = collect_candidate_import(
        import_config("lists/addapi.txt"), base_dir=tmp_path
    )
    _proxies, endpoints, issues = parse_raw_item(items[0])
    assert issues == []
    remarks = {node.address: node.remarks for node in endpoints}
    assert remarks["150.230.206.130"] == "JP"
    assert remarks["2606:4700:3037:e1:a64b:6580:941f:e09"] == "官方优选IPv6"


def test_import_dedupes_with_this_run_cf_candidates(tmp_path: Path):
    # this-run CF list and the imported list share 150.230.206.130:443
    write(tmp_path / "cf" / "cf.txt", "150.230.206.130:443#local-cf\n203.0.113.9:443\n")
    write(tmp_path / "lists" / "addapi.txt", ADDAPI_TEXT)

    cf_cfg = AppConfig(
        sources=SourcesConfig(
            cf=CfSourceConfig(
                enabled=True,
                candidate_files=["cf/cf.txt"],
            ),
            candidate_import=CandidateImportConfig(
                enabled=True,
                files=["lists/addapi.txt"],
            ),
        )
    )
    cf_items, _ = collect_cf(cf_cfg, base_dir=tmp_path)
    imp_items, _ = collect_candidate_import(cf_cfg, base_dir=tmp_path)

    proxies = []
    endpoints = []
    issues = []
    for item in [*cf_items, *imp_items]:
        p, e, i = parse_raw_item(item)
        proxies.extend(p)
        endpoints.extend(e)
        issues.extend(i)
    assert issues == []
    nodes, edges, _ = normalize_all(proxies, endpoints, issues)
    final_proxies, final_edges = dedupe(nodes, edges)

    addresses = sorted(edge.address for edge in final_edges)
    # 203.0.113.9 (cf only), 150.230.206.130 (merged), 151.145.67.90, and v6
    assert "150.230.206.130" in addresses
    assert addresses.count("150.230.206.130") == 1
    merged = next(edge for edge in final_edges if edge.address == "150.230.206.130")
    assert set(merged.source_ids) == {"cf", "imported"}
    assert len(final_edges) == 4


def test_cli_collect_import_candidates(tmp_path: Path, monkeypatch, capsys):
    from nodebench.cli.main import main

    write(tmp_path / "lists" / "addapi.txt", ADDAPI_TEXT)
    out_dir = tmp_path / "out"
    # use a profile that does not require external probes
    code = main(
        [
            "collect",
            "--import-candidates",
            str(tmp_path / "lists" / "addapi.txt"),
        ]
    )
    captured = capsys.readouterr()
    assert "imported" in captured.out
    assert "raw_items=" in captured.out
    assert code in (0, 2, 3, 4)


def test_run_pipeline_import_source_report(tmp_path: Path):
    write(tmp_path / "lists" / "addcsv.csv", ADDCSV_TEXT)
    config = import_config(str(tmp_path / "lists" / "addcsv.csv"))
    config.output_dir = str(tmp_path / "out")
    ctx = RunContext.create(profile="import-test", runner_id="test:import")
    result = run_pipeline(config, ctx, dry_run=True, run_probes=False)
    reports = result["source_reports"]
    assert len(reports) == 1
    assert reports[0]["source_id"] == "imported"
    assert reports[0]["ok"] is True
    assert result["counts"]["edge_endpoints"] == 2
    # imported measurements must not appear as ranked latency/speed
    for preview in result["items_preview"]["edge_endpoints"]:
        assert preview["address"]
