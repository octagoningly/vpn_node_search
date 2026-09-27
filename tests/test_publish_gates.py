from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

from nodebench.core.config import load_config
from nodebench.core.schema import PublishResult
from nodebench.core.stages import run_publish_stage
from nodebench.exporters.build import build_export, stage_view
from nodebench.exporters.cf_addapi import CF_ADDAPI_NAME
from nodebench.exporters.cf_addcsv import CF_ADDCSV_NAME
from nodebench.exporters.clash import PROXY_CLASH_NAME
from nodebench.exporters.manifest import MANIFEST_NAME
from nodebench.exporters.raw import PROXY_RAW_NAME
from nodebench.exporters.report import REPORT_NAME
from nodebench.publishing.publish import (
    REASON_CF_NOT_AUTHORIZED,
    REASON_LICENSE,
    REASON_PROXY_CREDENTIALS,
    publish_output,
)

from test_exporters import (
    make_edge,
    make_node,
    make_ranked_endpoint,
    make_ranked_proxy,
    make_report,
)
from test_publish import ALL_FILES, build_fixture, publish

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PATH = PROJECT_ROOT / "config" / "default.yaml"
RUN_ID = "20260101T000000Z-abcdef"


def manifest_of(directory: Path) -> dict:
    return json.loads((directory / MANIFEST_NAME).read_text(encoding="utf-8"))


def published_names(directory: Path) -> list[str]:
    return sorted(path.name for path in directory.iterdir() if path.is_file())


def test_private_export_and_public_publish_carry_opposite_visibility(tmp_path: Path):
    export_dir, target, report, outcome = build_fixture(tmp_path)
    view = stage_view(outcome)
    assert view["visibility"] == "private"
    assert "publish" not in view

    exported = json.loads((export_dir / REPORT_NAME).read_text(encoding="utf-8"))
    assert exported["stages"]["export"]["visibility"] == "private"
    assert "publish" not in exported["stages"]

    result = publish(export_dir, target, report, outcome)
    assert result.status == "ok"
    assert result.visibility == "public"
    published = json.loads((target / REPORT_NAME).read_text(encoding="utf-8"))
    assert published["stages"]["export"]["visibility"] == "private"
    assert published["stages"]["publish"]["visibility"] == "public"

    assert published_names(export_dir) == ALL_FILES


def test_proxy_credentials_gate_excludes_from_public_only(tmp_path: Path):
    export_dir, target, report, outcome = build_fixture(tmp_path)
    result = publish(
        export_dir, target, report, outcome, allow_proxy_credentials=False
    )
    assert result.status == "ok"
    assert set(result.excluded) == {PROXY_RAW_NAME, PROXY_CLASH_NAME}
    assert result.excluded_reasons[PROXY_RAW_NAME] == REASON_PROXY_CREDENTIALS
    assert result.excluded_reasons[PROXY_CLASH_NAME] == REASON_PROXY_CREDENTIALS

    names = published_names(target)
    assert PROXY_RAW_NAME not in names
    assert PROXY_CLASH_NAME not in names
    assert CF_ADDAPI_NAME in names
    assert published_names(export_dir) == ALL_FILES


def test_proxy_credentials_published_when_allowed(tmp_path: Path):
    export_dir, target, report, outcome = build_fixture(tmp_path)
    result = publish(export_dir, target, report, outcome, allow_proxy_credentials=True)
    assert result.status == "ok"
    assert result.excluded == []
    assert PROXY_RAW_NAME in result.files
    assert PROXY_CLASH_NAME in result.files
    assert (target / PROXY_RAW_NAME).read_bytes() == (
        export_dir / PROXY_RAW_NAME
    ).read_bytes()


def test_user_supplied_cf_candidates_need_explicit_authorization(tmp_path: Path):
    export_dir, target, report, outcome = build_fixture(tmp_path)
    result = publish(
        export_dir, target, report, outcome, endpoint_license_tags={"user_supplied"}
    )
    assert result.status == "ok"
    assert result.cf_candidates_user_supplied is True
    assert result.cf_candidates_authorized is False
    assert set(result.excluded) == {CF_ADDAPI_NAME, CF_ADDCSV_NAME}
    assert result.excluded_reasons[CF_ADDAPI_NAME] == REASON_CF_NOT_AUTHORIZED
    assert CF_ADDAPI_NAME not in published_names(target)
    assert CF_ADDAPI_NAME in published_names(export_dir)

    manifest = manifest_of(target)
    assert manifest["cf_candidates_user_supplied"] is True
    assert manifest["cf_candidates_authorized"] is False


def test_user_supplied_cf_candidates_publish_when_authorized(tmp_path: Path):
    export_dir, target, report, outcome = build_fixture(tmp_path)
    result = publish(
        export_dir,
        target,
        report,
        outcome,
        endpoint_license_tags={"user_supplied"},
        cf_candidates_authorized=True,
    )
    assert result.status == "ok"
    assert result.excluded == []
    assert result.cf_candidates_user_supplied is True
    assert result.cf_candidates_authorized is True
    assert CF_ADDAPI_NAME in result.files
    assert CF_ADDCSV_NAME in result.files
    assert published_names(target) == ALL_FILES

    manifest = manifest_of(target)
    assert manifest["cf_candidates_user_supplied"] is True
    assert manifest["cf_candidates_authorized"] is True


def test_authorized_cf_candidates_keep_the_license_gate(tmp_path: Path):
    export_dir, target, report, outcome = build_fixture(tmp_path)
    result = publish(
        export_dir,
        target,
        report,
        outcome,
        endpoint_license_tags={"user_supplied", "proprietary"},
        cf_candidates_authorized=True,
    )
    assert result.status == "ok"
    assert set(result.excluded) == {CF_ADDAPI_NAME, CF_ADDCSV_NAME}
    assert result.excluded_reasons[CF_ADDCSV_NAME] == REASON_LICENSE
    assert CF_ADDAPI_NAME not in published_names(target)
    assert result.cf_candidates_user_supplied is True
    assert result.cf_candidates_authorized is True


def test_public_manifest_describes_exactly_the_files_on_disk(tmp_path: Path):
    export_dir, target, report, outcome = build_fixture(tmp_path)
    export_manifest = manifest_of(export_dir)
    entry_counts = {
        entry["name"]: entry["entry_count"] for entry in export_manifest["files"]
    }
    result = publish(
        export_dir,
        target,
        report,
        outcome,
        allow_proxy_credentials=False,
        endpoint_license_tags={"user_supplied"},
    )
    assert result.status == "ok"

    manifest = manifest_of(target)
    disk = published_names(target)
    listed = [entry["name"] for entry in manifest["files"]]
    assert sorted(listed) == [name for name in disk if name != MANIFEST_NAME]
    assert MANIFEST_NAME not in listed
    assert manifest["counts"]["files"] == len(disk)
    assert manifest["publishable"] is True
    assert manifest["run_id"] == report["run_id"]
    assert manifest["counts"]["proxies"] == export_manifest["counts"]["proxies"]
    assert manifest["counts"]["endpoints"] == export_manifest["counts"]["endpoints"]
    for entry in manifest["files"]:
        payload = (target / entry["name"]).read_bytes()
        assert entry["sha256"] == hashlib.sha256(payload).hexdigest()
        assert entry["size_bytes"] == len(payload)
        assert entry["entry_count"] == entry_counts[entry["name"]]


def test_publish_configuration_defaults_disable_publishing(tmp_path: Path):
    config = load_config(DEFAULT_PATH, None, env={})
    assert config.publish.enabled is False
    assert config.publish.allow_proxy_credentials is False
    assert config.publish.cf_candidates_authorized is False

    export_dir, target, report, outcome = build_fixture(tmp_path)
    result = publish_output(
        export_dir=export_dir,
        target_dir=target,
        report=report,
        outcome=outcome,
        ranked_proxies=1,
        ranked_endpoints=1,
        proxy_license_tags={"mit"},
        endpoint_license_tags={"mit"},
        enabled=config.publish.enabled,
        allow_publish=True,
        allow_proxy_credentials=config.publish.allow_proxy_credentials,
        cf_candidates_authorized=config.publish.cf_candidates_authorized,
    )
    assert result.status == "skipped"
    assert result.reason == "disabled"
    assert target.exists() is False
    assert list(tmp_path.glob("**/latest")) == []


def test_publish_stage_reads_publish_configuration(tmp_path: Path, monkeypatch):
    import nodebench.core.stages as stages_module

    config = load_config(DEFAULT_PATH, None, env={})
    config.output_dir = str(tmp_path / "out")
    config.publish.enabled = True
    config.publish.allow_proxy_credentials = True
    config.publish.cf_candidates_authorized = True

    captured: dict[str, object] = {}

    def capture(**kwargs: object) -> PublishResult:
        captured.update(kwargs)
        return PublishResult(status="skipped", reason="disabled")

    monkeypatch.setattr(stages_module, "publish_output", capture)
    export_dir = stages_module.export_dir(config, RUN_ID)
    outcome = build_export(
        export_dir,
        make_report(run_id=RUN_ID),
        nodes=[make_node()],
        edges=[make_edge()],
        proxies=[make_ranked_proxy()],
        endpoints=[make_ranked_endpoint()],
        scoring_version="1",
        cf_candidates_authorized=True,
    )
    score_report = SimpleNamespace(
        proxies=[SimpleNamespace(status="ranked", source_ids=["local"])],
        endpoints=[SimpleNamespace(status="ranked", source_ids=["cf"])],
    )
    summary = run_publish_stage(
        config,
        make_report(run_id=RUN_ID),
        outcome=outcome,
        score_report=score_report,
        licenses=[
            {"source_id": "cf", "license_tag": "user_supplied", "redistributable": False},
            {"source_id": "local", "license_tag": "unknown", "redistributable": False},
        ],
        allow_publish=True,
    )

    assert captured["enabled"] is True
    assert captured["allow_publish"] is True
    assert captured["allow_proxy_credentials"] is True
    assert captured["cf_candidates_authorized"] is True
    assert captured["proxy_license_tags"] == {"unknown"}
    assert captured["endpoint_license_tags"] == {"user_supplied"}
    assert Path(str(captured["target_dir"])) == tmp_path / "out" / "latest"
    assert summary["status"] == "skipped"
    assert summary["visibility"] == "public"


def test_export_manifest_records_user_supplied_cf_candidates(tmp_path: Path):
    report = make_report(
        licenses=[
            {"source_id": "cf", "license_tag": "user_supplied", "redistributable": False}
        ]
    )
    outcome = build_export(
        tmp_path / "export",
        report,
        nodes=[make_node()],
        edges=[make_edge()],
        proxies=[make_ranked_proxy()],
        endpoints=[make_ranked_endpoint()],
        scoring_version="1",
    )
    manifest = manifest_of(Path(outcome.directory))
    assert manifest["cf_candidates_user_supplied"] is True
    assert manifest["cf_candidates_authorized"] is False

    authorized = build_export(
        tmp_path / "export-authorized",
        report,
        nodes=[make_node()],
        edges=[make_edge()],
        proxies=[make_ranked_proxy()],
        endpoints=[make_ranked_endpoint()],
        scoring_version="1",
        cf_candidates_authorized=True,
    )
    authorized_manifest = manifest_of(Path(authorized.directory))
    assert authorized_manifest["cf_candidates_user_supplied"] is True
    assert authorized_manifest["cf_candidates_authorized"] is True
