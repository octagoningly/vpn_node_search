from __future__ import annotations

from pathlib import Path

from nodebench.core.config import AppConfig, load_config
from nodebench.core.context import build_run_context
from nodebench.core.orchestrator import run_pipeline
from nodebench.core.serialization import dumps_json

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONFIG_DIR = PROJECT_ROOT / "config"
DEFAULT_PATH = CONFIG_DIR / "default.yaml"
PROFILE_DIR = CONFIG_DIR / "profiles"
INPUT_DIR = PROJECT_ROOT / "input"

URI_TEXT = (
    "vless://123e4567-e89b-12d3-a456-426614174000@192.0.2.10:443"
    "?type=tcp&security=tls&host=node.example.test#sample-192-0-2-10\n"
)
DUP_TEXT = (
    "vless://123e4567-e89b-12d3-a456-426614174000@192.0.2.10:443"
    "?type=tcp&security=tls#sample-duplicate\n"
)
CLEAN_FILES = (
    "sample-uri-list.txt",
    "sample-base64.txt",
    "sample-clash.yaml",
    "sample-endpoints.csv",
    "cf-candidates.csv",
)

PROXY_PREVIEW_KEYS = {
    "item_id",
    "kind",
    "protocol",
    "port",
    "transport",
    "security",
    "source_ids",
    "remarks",
}
ENDPOINT_PREVIEW_KEYS = {
    "item_id",
    "kind",
    "address",
    "port",
    "target_host",
    "tls",
    "source_ids",
    "remarks",
}


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))
    return path


def collect_keys(node, found: set[str] | None = None) -> set[str]:
    keys = found if found is not None else set()
    if isinstance(node, dict):
        for key, value in node.items():
            keys.add(str(key))
            collect_keys(value, keys)
    elif isinstance(node, list):
        for value in node:
            collect_keys(value, keys)
    return keys


def local_config(paths: list[Path] | None = None) -> AppConfig:
    config = load_config(DEFAULT_PATH, PROFILE_DIR / "local.yaml", env={})
    if paths is not None:
        config.sources.local.paths = [str(path) for path in paths]
    return config


def run(config: AppConfig, dry_run: bool = True) -> dict:
    ctx = build_run_context(config, {"profile": config.profile})
    return run_pipeline(config, ctx, dry_run=dry_run)


def test_full_input_sample_counts():
    result = run(local_config([INPUT_DIR]))
    assert result["counts"] == {
        "sources_total": 1,
        "sources_failed": 0,
        "raw_items": 6,
        "parsed_proxies": 7,
        "parsed_endpoints": 6,
        "parse_issues": 1,
        "proxy_nodes": 7,
        "edge_endpoints": 6,
        "dupes_merged": 0,
    }
    assert result["status"] == "partial"
    assert result["diagnostics"] == [
        "local:unsupported_content: payload has no uri lines"
    ]
    report = result["source_reports"][0]
    assert report["source_id"] == "local"
    assert report["ok"] is True
    assert report["fetched"] == 6


def test_clean_inputs_report_ok_status():
    paths = [INPUT_DIR / name for name in CLEAN_FILES]
    result = run(local_config(paths))
    counts = result["counts"]
    assert counts["parse_issues"] == 0
    assert counts["proxy_nodes"] == 7
    assert counts["edge_endpoints"] == 6
    assert counts["dupes_merged"] == 0
    assert result["status"] == "ok"
    assert result["issues"] == []
    assert result["diagnostics"] == []


def test_missing_input_path_fails_the_run(tmp_path: Path):
    result = run(local_config([tmp_path / "absent"]))
    counts = result["counts"]
    assert counts["sources_total"] == 1
    assert counts["sources_failed"] == 1
    assert counts["raw_items"] == 0
    assert counts["proxy_nodes"] == 0
    assert result["status"] == "failed"
    assert result["source_reports"][0]["ok"] is False
    assert result["source_reports"][0]["errors"][0]["code"] == "path_missing"
    assert result["limits"]["failed_sources"] == ["local"]


def test_disabled_sources_report_failure():
    config = load_config(DEFAULT_PATH, None, env={})
    config.sources.local.enabled = False
    result = run(config)
    assert result["status"] == "failed"
    assert result["counts"]["sources_total"] == 0
    assert result["limits"]["disabled_sources"] == [
        "local",
        "subscriptions",
        "github",
        "cf",
    ]


def test_duplicate_nodes_merge_into_one(tmp_path: Path):
    first = write(tmp_path / "a" / "nodes.txt", DUP_TEXT)
    second = write(tmp_path / "b" / "nodes.txt", DUP_TEXT)
    result = run(local_config([first, second]))
    counts = result["counts"]
    assert counts["parsed_proxies"] == 2
    assert counts["proxy_nodes"] == 1
    assert counts["dupes_merged"] == 1
    assert counts["parse_issues"] == 0
    assert result["status"] == "ok"
    assert result["items_preview"]["proxy_nodes"][0]["source_ids"] == ["local"]


def test_items_preview_is_whitelisted_and_redacted():
    paths = [INPUT_DIR / name for name in CLEAN_FILES]
    result = run(local_config(paths))
    preview = result["items_preview"]
    assert len(preview["proxy_nodes"]) == 7
    assert len(preview["edge_endpoints"]) == 6
    for node in preview["proxy_nodes"]:
        assert set(node) == PROXY_PREVIEW_KEYS
        assert node["item_id"].startswith("hmac-sha256:")
        assert node["kind"] == "proxy_node"
        assert isinstance(node["port"], int)
    for edge in preview["edge_endpoints"]:
        assert set(edge) == ENDPOINT_PREVIEW_KEYS
        assert edge["kind"] == "edge_endpoint"
        assert edge["target_host"] is None
    text = dumps_json(result)
    assert "sample-password" not in text
    assert "123e4567-e89b-12d3-a456-426614174000" not in text
    assert collect_keys(result).isdisjoint({"secrets", "server", "params", "payload"})


def test_dry_run_flag_and_limits():
    paths = [INPUT_DIR / name for name in CLEAN_FILES]
    dry_result = run(local_config(paths), dry_run=True)
    wet_result = run(local_config(paths), dry_run=False)
    assert dry_result["dry_run"] is True
    assert wet_result["dry_run"] is False
    assert dry_result["stages_pending"] == [
        "probe",
        "inspect",
        "persist",
        "score",
        "export",
        "publish",
    ]
    limits = dry_result["limits"]
    assert limits["caps"] == {
        "cf_max_ips_per_run": 200,
        "github_max_files_per_run": 30,
    }
    assert limits["budget"] == {}
    assert limits["truncated_sources"] == []
    assert limits["failed_sources"] == []
    assert limits["disabled_sources"] == ["subscriptions", "github", "cf"]
