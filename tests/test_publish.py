from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from nodebench.core.schema import ExportOutcome, ExportedFile, ValidationReport
from nodebench.exporters.build import build_export
from nodebench.exporters.cf_addapi import CF_ADDAPI_NAME
from nodebench.exporters.cf_addcsv import CF_ADDCSV_NAME
from nodebench.exporters.clash import PROXY_CLASH_NAME
from nodebench.exporters.manifest import MANIFEST_NAME
from nodebench.exporters.raw import PROXY_RAW_NAME
from nodebench.exporters.report import REPORT_NAME
from nodebench.publishing import REDISTRIBUTIBLE_LICENSE_TAGS, publish_output
from nodebench.publishing.publish import is_redistributable

from test_exporters import (
    make_edge,
    make_node,
    make_ranked_endpoint,
    make_ranked_proxy,
    make_report,
)

ALL_FILES = sorted(
    [
        PROXY_CLASH_NAME,
        PROXY_RAW_NAME,
        CF_ADDAPI_NAME,
        CF_ADDCSV_NAME,
        REPORT_NAME,
        MANIFEST_NAME,
    ]
)


def build_fixture(tmp_path: Path) -> tuple[Path, Path, dict, ExportOutcome]:
    out = tmp_path / "out"
    export_dir = out / "run" / "export"
    report = make_report()
    outcome = build_export(
        export_dir,
        report,
        nodes=[make_node()],
        edges=[make_edge()],
        proxies=[make_ranked_proxy()],
        endpoints=[make_ranked_endpoint()],
        scoring_version="1",
    )
    assert outcome.status == "ok"
    exported = json.loads((export_dir / REPORT_NAME).read_text(encoding="utf-8"))
    assert exported["stages"]["export"]["status"] == "ok"
    return export_dir, out / "latest", exported, outcome


def publish(
    export_dir: Path,
    target: Path,
    report: dict,
    outcome: ExportOutcome,
    **kwargs: object,
):
    payload: dict = {
        "export_dir": export_dir,
        "target_dir": target,
        "report": report,
        "outcome": outcome,
        "ranked_proxies": 1,
        "ranked_endpoints": 1,
        "proxy_license_tags": {"mit"},
        "endpoint_license_tags": {"mit"},
        "enabled": True,
        "allow_publish": True,
        "allow_proxy_credentials": True,
    }
    payload.update(kwargs)
    return publish_output(**payload)


def test_redistributable_license_tag_set():
    assert "mit" in REDISTRIBUTIBLE_LICENSE_TAGS
    assert "apache-2.0" in REDISTRIBUTIBLE_LICENSE_TAGS
    assert "public-domain" in REDISTRIBUTIBLE_LICENSE_TAGS
    assert "unknown" not in REDISTRIBUTIBLE_LICENSE_TAGS
    assert "proprietary" not in REDISTRIBUTIBLE_LICENSE_TAGS
    assert is_redistributable("MIT") is True
    assert is_redistributable("  Mpl-2.0 ") is True
    assert is_redistributable("unknown") is False
    assert is_redistributable("") is False


def test_publish_disabled_is_skipped(tmp_path: Path):
    export_dir, target, report, outcome = build_fixture(tmp_path)
    result = publish(export_dir, target, report, outcome, enabled=False)
    assert result.status == "skipped"
    assert result.reason == "disabled"
    assert target.exists() is False


def test_publish_no_publish_flag_is_skipped(tmp_path: Path):
    export_dir, target, report, outcome = build_fixture(tmp_path)
    result = publish(export_dir, target, report, outcome, allow_publish=False)
    assert result.status == "skipped"
    assert result.reason == "no_publish_flag"
    assert target.exists() is False


def test_publish_success_creates_latest_atomically(tmp_path: Path):
    export_dir, target, report, outcome = build_fixture(tmp_path)
    result = publish(export_dir, target, report, outcome)
    assert result.status == "ok"
    assert result.reason == ""
    assert result.errors == []
    assert result.blocked == []
    assert result.replaced_previous is False
    assert Path(result.path) == target
    assert sorted(path.name for path in target.iterdir()) == ALL_FILES
    assert sorted(result.files) == ALL_FILES
    assert list(target.parent.glob(".latest*")) == []


def test_publish_second_run_replaces_previous_target(tmp_path: Path):
    export_dir, target, report, outcome = build_fixture(tmp_path)
    first = publish(export_dir, target, report, outcome)
    assert first.status == "ok"
    stale = target / "stale.txt"
    stale.write_text("old", encoding="utf-8")

    second = publish(export_dir, target, report, outcome)
    assert second.status == "ok"
    assert second.replaced_previous is True
    assert not stale.exists()
    assert sorted(path.name for path in target.iterdir()) == ALL_FILES
    assert list(target.parent.glob(".latest*")) == []


def test_published_report_carries_publish_stage(tmp_path: Path):
    export_dir, target, report, outcome = build_fixture(tmp_path)
    result = publish(export_dir, target, report, outcome)
    assert result.status == "ok"
    payload = json.loads((target / REPORT_NAME).read_text(encoding="utf-8"))
    assert payload["stages"]["publish"]["status"] == "ok"
    assert payload["stages"]["export"]["status"] == "ok"
    assert "publish" not in payload["stages_pending"]
    assert payload["counts"] == make_report()["counts"]


def test_published_manifest_report_digest_matches(tmp_path: Path):
    export_dir, target, report, outcome = build_fixture(tmp_path)
    result = publish(export_dir, target, report, outcome)
    assert result.status == "ok"
    manifest = json.loads((target / MANIFEST_NAME).read_text(encoding="utf-8"))
    entry = next(
        item for item in manifest["files"] if item["name"] == REPORT_NAME
    )
    payload = (target / REPORT_NAME).read_bytes()
    assert entry["sha256"] == hashlib.sha256(payload).hexdigest()
    assert entry["size_bytes"] == len(payload)


def test_publish_blocked_when_probe_is_degraded(tmp_path: Path):
    export_dir, target, report, outcome = build_fixture(tmp_path)
    report["probe"] = {"proxy": {"mode": "skip", "skipped_reason": "missing_binary"}}
    result = publish(export_dir, target, report, outcome)
    assert result.status == "blocked"
    assert "probe_incomplete" in result.blocked
    assert target.exists() is False


def test_publish_blocked_without_ranked_items(tmp_path: Path):
    export_dir, target, report, outcome = build_fixture(tmp_path)
    result = publish(
        export_dir, target, report, outcome, ranked_proxies=0, ranked_endpoints=0
    )
    assert result.status == "blocked"
    assert "zero_valid_items" in result.blocked
    assert target.exists() is False


def test_publish_blocked_when_export_failed(tmp_path: Path):
    export_dir, target, report, _outcome = build_fixture(tmp_path)
    failed = ExportOutcome(
        status="failed",
        directory=str(export_dir),
        files=[],
        errors=["io_error"],
        validation=None,
        publishable=False,
        counts={},
    )
    result = publish(export_dir, target, report, failed)
    assert result.status == "blocked"
    assert result.blocked[0] == "export_failed"
    assert "validation_failed" in result.blocked
    assert target.exists() is False


def test_publish_blocked_when_validation_failed(tmp_path: Path):
    export_dir, target, report, outcome = build_fixture(tmp_path)
    outcome = outcome.model_copy(
        update={
            "validation": ValidationReport(
                ok=False,
                errors=["credential uri in report.json"],
                files_checked=[REPORT_NAME],
            ),
            "publishable": False,
        }
    )
    result = publish(export_dir, target, report, outcome)
    assert result.status == "blocked"
    assert "validation_failed" in result.blocked
    assert "nothing_publishable" in result.blocked
    assert target.exists() is False


def test_publish_blocked_when_every_source_failed(tmp_path: Path):
    export_dir, target, report, outcome = build_fixture(tmp_path)
    report["counts"] = dict(report["counts"], sources_total=2, sources_failed=2)
    result = publish(export_dir, target, report, outcome)
    assert result.status == "blocked"
    assert "critical_source_failure" in result.blocked


def test_publish_blocked_when_proxy_license_not_redistributable(tmp_path: Path):
    export_dir, target, report, outcome = build_fixture(tmp_path)
    result = publish(export_dir, target, report, outcome, proxy_license_tags={"unknown"})
    assert result.status == "blocked"
    assert "license_not_redistributable" in result.blocked
    assert PROXY_RAW_NAME in result.excluded
    assert PROXY_CLASH_NAME in result.excluded
    assert target.exists() is False


def test_publish_excludes_endpoints_with_unknown_license_but_publishes(tmp_path: Path):
    export_dir, target, report, outcome = build_fixture(tmp_path)
    result = publish(
        export_dir, target, report, outcome, endpoint_license_tags={"operator-supplied"}
    )
    assert result.status == "ok"
    assert CF_ADDAPI_NAME in result.excluded
    assert CF_ADDCSV_NAME in result.excluded
    assert REPORT_NAME not in result.excluded
    assert MANIFEST_NAME not in result.excluded
    assert sorted(result.files) == sorted(
        [PROXY_CLASH_NAME, PROXY_RAW_NAME, REPORT_NAME, MANIFEST_NAME]
    )
    published = sorted(path.name for path in target.iterdir())
    assert published == sorted(result.files)


def test_publish_never_excludes_report_and_manifest(tmp_path: Path):
    export_dir, target, report, outcome = build_fixture(tmp_path)
    outcome = outcome.model_copy(
        update={
            "files": [
                ExportedFile(
                    name=REPORT_NAME, sha256="0" * 64, size_bytes=1, entry_count=0
                ),
                ExportedFile(
                    name=MANIFEST_NAME, sha256="1" * 64, size_bytes=1, entry_count=0
                ),
            ]
        }
    )
    result = publish(
        export_dir,
        target,
        report,
        outcome,
        proxy_license_tags=set(),
        endpoint_license_tags=set(),
    )
    assert REPORT_NAME not in result.excluded
    assert MANIFEST_NAME not in result.excluded
    assert PROXY_RAW_NAME in result.excluded
    assert CF_ADDAPI_NAME in result.excluded


def test_publish_ignores_zero_entry_content_files(tmp_path: Path):
    export_dir, target, report, outcome = build_fixture(tmp_path)
    outcome = outcome.model_copy(
        update={
            "files": [
                ExportedFile(
                    name=PROXY_RAW_NAME, sha256="0" * 64, size_bytes=0, entry_count=0
                ),
                ExportedFile(
                    name=PROXY_CLASH_NAME, sha256="1" * 64, size_bytes=0, entry_count=0
                ),
                ExportedFile(
                    name=CF_ADDAPI_NAME, sha256="2" * 64, size_bytes=0, entry_count=0
                ),
                ExportedFile(
                    name=CF_ADDCSV_NAME, sha256="3" * 64, size_bytes=0, entry_count=0
                ),
                ExportedFile(
                    name=REPORT_NAME, sha256="4" * 64, size_bytes=1, entry_count=0
                ),
                ExportedFile(
                    name=MANIFEST_NAME, sha256="5" * 64, size_bytes=1, entry_count=0
                ),
            ]
        }
    )
    result = publish(export_dir, target, report, outcome)
    assert result.status == "ok"
    assert PROXY_RAW_NAME in result.excluded
    assert CF_ADDCSV_NAME in result.excluded
    assert REPORT_NAME not in result.excluded
    assert MANIFEST_NAME not in result.excluded
    assert sorted(result.files) == sorted([REPORT_NAME, MANIFEST_NAME])
    assert sorted(path.name for path in target.iterdir()) == sorted(result.files)


def test_publish_result_is_json_serializable(tmp_path: Path):
    export_dir, target, report, outcome = build_fixture(tmp_path)
    result = publish(export_dir, target, report, outcome)
    from nodebench.core.serialization import public_dump

    dumped = public_dump(result)
    assert dumped["status"] == "ok"
    assert json.loads(json.dumps(dumped))["path"] == str(target)
