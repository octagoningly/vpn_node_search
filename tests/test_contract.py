from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest

from nodebench.core.config import load_config
from nodebench.core.context import build_run_context
from nodebench.core.orchestrator import run_pipeline
from nodebench.core.schema import RUN_ID_PATTERN, SCHEMA_VERSION
from nodebench.core.serialization import dumps_json, is_forbidden_key, public_dump

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONFIG_DIR = PROJECT_ROOT / "config"
DEFAULT_PATH = CONFIG_DIR / "default.yaml"
PROFILE_DIR = CONFIG_DIR / "profiles"
INPUT_DIR = PROJECT_ROOT / "input"

TOP_LEVEL_KEYS = {
    "schema_version",
    "run_id",
    "runner_id",
    "profile",
    "generated_at",
    "dry_run",
    "stages_pending",
    "stages",
    "licenses",
    "status",
    "diagnostics",
    "counts",
    "protocol_support",
    "limits",
    "source_reports",
    "issues",
    "items_preview",
    "probe",
}
STAGES_PENDING = ["inspect", "persist", "score", "export", "publish"]
STATUSES = {"ok", "partial", "failed"}
SNAKE_CASE = re.compile(r"^[a-z][a-z0-9_]*$")
GENERATED_AT = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
ERROR_KEYS = {"stage", "code", "message_redacted", "retryable"}


def walk(node: Any, path: str = ""):
    if isinstance(node, dict):
        for key, value in node.items():
            child = f"{path}.{key}" if path else str(key)
            yield child, key, value
            yield from walk(value, child)
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from walk(value, f"{path}[{index}]")


@pytest.fixture(scope="module")
def result() -> dict:
    config = load_config(DEFAULT_PATH, PROFILE_DIR / "local.yaml", env={})
    config.sources.local.paths = [str(INPUT_DIR)]
    ctx = build_run_context(config, {"profile": config.profile})
    return run_pipeline(config, ctx, dry_run=True)


def test_top_level_contract(result: dict):
    assert set(result) == TOP_LEVEL_KEYS
    assert result["schema_version"] == SCHEMA_VERSION == 1
    assert result["profile"] == "local"
    assert result["dry_run"] is True
    assert result["stages_pending"] == STAGES_PENDING
    assert result["status"] in STATUSES


def test_run_id_and_timestamps(result: dict):
    assert re.match(RUN_ID_PATTERN, result["run_id"])
    assert GENERATED_AT.match(result["generated_at"])


def test_all_keys_are_snake_case(result: dict):
    for path, key, _ in walk(result):
        assert SNAKE_CASE.match(key), f"{path} uses a non snake_case key {key!r}"


def test_no_forbidden_keys(result: dict):
    for path, key, _ in walk(result):
        assert not is_forbidden_key(str(key)), f"{path} exposes {key!r}"


def test_result_is_already_public(result: dict):
    assert public_dump(result) == result
    assert "sample-password" not in dumps_json(result)
    assert "123e4567-e89b-12d3-a456-426614174000" not in dumps_json(result)


def test_counts_shape(result: dict):
    assert set(result["counts"]) == {
        "sources_total",
        "sources_failed",
        "raw_items",
        "parsed_proxies",
        "parsed_endpoints",
        "parse_issues",
        "proxy_nodes",
        "edge_endpoints",
        "dupes_merged",
    }
    for value in result["counts"].values():
        assert isinstance(value, int) and not isinstance(value, bool)
        assert value >= 0


def test_limits_shape(result: dict):
    limits = result["limits"]
    assert set(limits) == {
        "budget",
        "caps",
        "truncated_sources",
        "failed_sources",
        "disabled_sources",
    }
    assert set(limits["caps"]) == {"cf_max_ips_per_run", "github_max_files_per_run"}
    assert limits["caps"]["cf_max_ips_per_run"] == 200
    assert limits["caps"]["github_max_files_per_run"] == 30


def test_source_reports_and_errors_shape(result: dict):
    assert result["source_reports"], "sample run must record at least one report"
    for report in result["source_reports"]:
        assert set(report) == {
            "source_id",
            "ok",
            "fetched",
            "errors",
            "scope",
            "redacted",
            "mode",
            "etag",
            "last_modified",
        }
        assert isinstance(report["ok"], bool)
        for error in report["errors"]:
            assert set(error) == ERROR_KEYS
            assert isinstance(error["retryable"], bool)


def test_issues_shape(result: dict):
    for issue in result["issues"]:
        assert set(issue) == {
            "source_id",
            "code",
            "message_redacted",
            "raw_ref",
        }
        assert issue["code"]
        assert issue["message_redacted"]


def test_missing_values_use_null(result: dict):
    endpoints = result["items_preview"]["edge_endpoints"]
    assert endpoints
    for edge in endpoints:
        assert edge["target_host"] is None or isinstance(edge["target_host"], str)
        assert edge["target_host"] != 0
    assert result["limits"]["budget"] == {} or all(
        isinstance(value, float) for value in result["limits"]["budget"].values()
    )


def test_preview_items_match_entity_contract(result: dict):
    for node in result["items_preview"]["proxy_nodes"]:
        assert node["kind"] == "proxy_node"
        assert node["item_id"].startswith("hmac-sha256:")
        assert len(node["item_id"]) == len("hmac-sha256:") + 64
        assert isinstance(node["source_ids"], list) and node["source_ids"]
    for edge in result["items_preview"]["edge_endpoints"]:
        assert edge["kind"] == "edge_endpoint"
        assert edge["item_id"].startswith("hmac-sha256:")
        assert isinstance(edge["port"], int) and 1 <= edge["port"] <= 65535
        assert isinstance(edge["tls"], bool)
