from __future__ import annotations

from datetime import datetime, timezone

from nodebench.core.context import build_run_context
from nodebench.core.errors import StorageError
from nodebench.core.orchestrator import resolve_run_exit, run_pipeline
from nodebench.core.schema import RawItem
from nodebench.core.stages import license_entries, run_post_stages

from test_orchestrator import CLEAN_FILES, DEFAULT_PATH, INPUT_DIR, local_config

STAGE_PENDING = ["inspect", "persist", "score", "export", "publish"]


def make_report() -> dict:
    return {
        "run_id": "run-1",
        "runner_id": "tester",
        "profile": "default",
        "status": "ok",
        "counts": {"proxy_nodes": 1, "edge_endpoints": 0},
        "stages": {},
        "stages_pending": list(STAGE_PENDING),
    }


def make_items() -> list[RawItem]:
    moment = datetime(2026, 1, 1, tzinfo=timezone.utc)
    return [
        RawItem(
            source_id="b",
            content_type="text",
            payload="x",
            fetched_at=moment,
            license_tag="mit",
        ),
        RawItem(
            source_id="a",
            content_type="text",
            payload="y",
            fetched_at=moment,
            license_tag="mit",
        ),
        RawItem(source_id="b", content_type="text", payload="z", fetched_at=moment),
        RawItem(
            source_id="c",
            content_type="text",
            payload="w",
            fetched_at=moment,
            license_tag="proprietary",
        ),
        RawItem(source_id="d", content_type="text", payload="v", fetched_at=moment),
    ]


def test_license_entries_are_sorted_unique_and_default_unknown():
    entries = license_entries(make_items())
    assert [entry["source_id"] for entry in entries] == ["a", "b", "c", "d"]
    assert entries[0] == {
        "source_id": "a",
        "license_tag": "mit",
        "redistributable": True,
    }
    assert entries[1] == {
        "source_id": "b",
        "license_tag": "mit",
        "redistributable": True,
    }
    assert entries[2] == {
        "source_id": "c",
        "license_tag": "proprietary",
        "redistributable": False,
    }
    assert entries[3] == {
        "source_id": "d",
        "license_tag": "unknown",
        "redistributable": False,
    }


def test_stage_exit_uses_export_and_publish_semantics():
    from nodebench.core.orchestrator import _stage_exit

    assert _stage_exit({}) == 0
    assert _stage_exit({"persist": {"status": "ok"}}) == 0
    assert _stage_exit({"persist": {"status": "failed"}}) == 4
    assert _stage_exit({"score": {"status": "failed"}}) == 4
    assert _stage_exit({"export": {"status": "failed"}}) == 5
    assert _stage_exit({"publish": {"status": "blocked"}}) == 5
    assert _stage_exit({"publish": {"status": "failed"}}) == 5
    assert _stage_exit({"publish": {"status": "skipped"}}) == 0
    assert _stage_exit({"publish": {"status": "ok"}}) == 0


def test_resolve_run_exit_prefers_run_status_then_stages():
    assert resolve_run_exit(run_status="failed") == 3
    assert (
        resolve_run_exit(run_status="failed", stages={"export": {"status": "failed"}})
        == 3
    )
    assert resolve_run_exit(run_status="ok") == 0
    assert resolve_run_exit(run_status="ok", stages={}) == 0
    assert (
        resolve_run_exit(run_status="ok", stages={"inspect": {"status": "failed"}}) == 4
    )
    assert resolve_run_exit(run_status="ok", stages={"export": {"status": "failed"}}) == 5
    assert (
        resolve_run_exit(run_status="ok", stages={"publish": {"status": "ok"}}) == 0
    )
    assert (
        resolve_run_exit(run_status="ok", stages={"publish": {"status": "skipped"}})
        == 0
    )


def test_post_stages_stop_after_persist_failure(tmp_path, monkeypatch):
    from nodebench.core.config import load_config

    config = load_config(DEFAULT_PATH, None, env={})
    config.output_dir = str(tmp_path / "out")

    def boom(*_args, **_kwargs):
        raise StorageError(code="disk_full", message="no space left")

    monkeypatch.setattr("nodebench.core.stages.persist_run", boom)

    report = run_post_stages(
        make_report(),
        config=config,
        source_reports=[],
        nodes=[],
        edges=[],
        probe_results=[],
        items=make_items(),
    )
    assert report["stages"]["persist"] == {
        "status": "failed",
        "errors": ["disk_full: no space left"],
    }
    assert set(report["stages"]) == {"persist"}
    assert report["stages_pending"] == ["inspect", "score", "export", "publish"]
    assert report["licenses"] == [
        {"source_id": "a", "license_tag": "mit", "redistributable": True},
        {"source_id": "b", "license_tag": "mit", "redistributable": True},
        {"source_id": "c", "license_tag": "proprietary", "redistributable": False},
        {"source_id": "d", "license_tag": "unknown", "redistributable": False},
    ]


def test_wet_run_executes_post_stages(tmp_path):
    paths = [INPUT_DIR / name for name in CLEAN_FILES]
    config = local_config(paths)
    config.output_dir = str(tmp_path / "out")
    config.publish.enabled = False
    ctx = build_run_context(config, {"profile": config.profile})
    result = run_pipeline(
        config, ctx, dry_run=False, run_probes=False, post_stages=True
    )

    assert result["status"] == "ok"
    assert result["stages"]["persist"]["status"] == "ok"
    assert result["stages"]["score"]["status"] == "ok"
    assert result["stages"]["export"]["status"] == "ok"
    assert result["stages"]["publish"]["status"] == "skipped"
    assert "publish" not in result["stages_pending"]
    assert {entry["source_id"] for entry in result["licenses"]} == {"local"}
    assert isinstance(result["licenses"][0]["redistributable"], bool)
    assert (tmp_path / "out" / result["run_id"] / "scored.json").is_file()
    assert (tmp_path / "out" / result["run_id"] / "export" / "report.json").is_file()
    assert resolve_run_exit(run_status=result["status"], stages=result["stages"]) == 0
