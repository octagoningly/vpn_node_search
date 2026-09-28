from __future__ import annotations

import base64
import json
import urllib.error
from pathlib import Path

import pytest

from nodebench.cli.main import main
from nodebench.core.config import (
    GithubUploadConfig,
    HttpUploadConfig,
    PublishUploadConfig,
    load_config,
)
from nodebench.core.errors import ConfigError, PublishError
from nodebench.core.schema import ScoreReport
from nodebench.core.stages import (
    load_export_outcome,
    publish_artifacts,
    run_publish_stage,
)
from nodebench.exporters.cf_addapi import CF_ADDAPI_NAME
from nodebench.exporters.cf_addcsv import CF_ADDCSV_NAME
from nodebench.exporters.manifest import MANIFEST_NAME
from nodebench.exporters.raw import PROXY_RAW_NAME
from nodebench.exporters.report import REPORT_NAME
from nodebench.publishing.upload import (
    UPLOAD_WHITELIST,
    FolderUploader,
    GithubUploader,
    HttpUploader,
    create_uploader,
    prepare_payloads,
    select_uploadable,
    upload_published_files,
)

from test_exporters import (
    make_edge,
    make_node,
    make_ranked_endpoint,
    make_ranked_proxy,
    make_report,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PATH = PROJECT_ROOT / "config" / "default.yaml"
RUN_ID = "20260101T000000Z-abcdef"


class FakeHeaders(dict):
    def get(self, key, default=None):
        for k, v in self.items():
            if k.lower() == key.lower():
                return v
        return default


class FakeResponse:
    def __init__(self, body: bytes = b"{}", status: int = 200, headers: dict | None = None):
        self._body = body
        self._pos = 0
        self.status = status
        self.headers = FakeHeaders(headers or {})

    def getcode(self):
        return self.status

    def read(self, n: int = -1) -> bytes:
        if n is None or n < 0:
            chunk = self._body[self._pos :]
            self._pos = len(self._body)
            return chunk
        chunk = self._body[self._pos : self._pos + n]
        self._pos += len(chunk)
        return chunk

    def info(self):
        return self.headers

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def make_recorder(scripted: dict[str, object] | None = None, status: int = 200):
    """Record requests and optionally serve scripted responses per URL."""
    seen: list = []

    def opener(request, timeout=None):
        seen.append(request)
        if scripted is None:
            return FakeResponse(status=status)
        url = request.full_url if hasattr(request, "full_url") else str(request)
        if url not in scripted:
            raise AssertionError(f"unexpected URL: {url}")
        result = scripted[url]
        if isinstance(result, Exception):
            raise result
        return result

    return opener, seen


def whitelist_setup(tmp_path: Path) -> Path:
    publish_dir = tmp_path / "latest"
    publish_dir.mkdir()
    for name in sorted(UPLOAD_WHITELIST):
        (publish_dir / name).write_text(f"payload-{name}", encoding="utf-8")
    (publish_dir / PROXY_RAW_NAME).write_text("vless://secret", encoding="utf-8")
    (publish_dir / "history.db").write_bytes(b"sqlite")
    return publish_dir


# ---------------------------------------------------------------------------
# whitelist
# ---------------------------------------------------------------------------


def test_upload_whitelist_excludes_credential_files():
    assert CF_ADDAPI_NAME in UPLOAD_WHITELIST
    assert CF_ADDCSV_NAME in UPLOAD_WHITELIST
    assert REPORT_NAME in UPLOAD_WHITELIST
    assert MANIFEST_NAME in UPLOAD_WHITELIST
    assert PROXY_RAW_NAME not in UPLOAD_WHITELIST
    assert "proxy-clash.yaml" not in UPLOAD_WHITELIST
    assert "history.db" not in UPLOAD_WHITELIST


def test_prepare_payloads_rejects_credential_files(tmp_path: Path):
    publish_dir = whitelist_setup(tmp_path)
    with pytest.raises(PublishError) as info:
        prepare_payloads([publish_dir / PROXY_RAW_NAME])
    assert info.value.code == "upload_not_allowed"
    with pytest.raises(PublishError) as info:
        prepare_payloads([publish_dir / "history.db"])
    assert info.value.code == "upload_not_allowed"


def test_select_uploadable_skips_credential_files(tmp_path: Path):
    publish_dir = whitelist_setup(tmp_path)
    names = [path.name for path in select_uploadable(publish_dir)]
    assert names == sorted(UPLOAD_WHITELIST)
    assert PROXY_RAW_NAME not in names


def test_folder_uploader_rejects_credential_file(tmp_path: Path):
    publish_dir = whitelist_setup(tmp_path)
    destination = tmp_path / "mirror"
    uploader = FolderUploader(destination)
    with pytest.raises(PublishError) as info:
        uploader.upload_files([publish_dir / PROXY_RAW_NAME], RUN_ID)
    assert info.value.code == "upload_not_allowed"
    assert not (destination / PROXY_RAW_NAME).exists()
    assert not destination.exists() or not any(destination.iterdir())


def test_folder_uploader_rejects_before_copy_when_any_file_is_forbidden(
    tmp_path: Path,
):
    publish_dir = whitelist_setup(tmp_path)
    destination = tmp_path / "mirror"
    uploader = FolderUploader(destination)
    with pytest.raises(PublishError):
        uploader.upload_files(
            [publish_dir / CF_ADDAPI_NAME, publish_dir / PROXY_RAW_NAME], RUN_ID
        )
    assert not (destination / CF_ADDAPI_NAME).exists()


# ---------------------------------------------------------------------------
# folder backend
# ---------------------------------------------------------------------------


def test_folder_backend_success(tmp_path: Path):
    publish_dir = whitelist_setup(tmp_path)
    destination = tmp_path / "public"
    uploader = FolderUploader(destination)
    urls = uploader.upload_files(select_uploadable(publish_dir), RUN_ID)
    assert len(urls) == len(UPLOAD_WHITELIST)
    for name in sorted(UPLOAD_WHITELIST):
        copied = destination / name
        assert copied.read_text(encoding="utf-8") == f"payload-{name}"
        assert str(copied.resolve()) in urls


# ---------------------------------------------------------------------------
# github backend
# ---------------------------------------------------------------------------


def github_config(**overrides: object) -> PublishUploadConfig:
    payload: dict = {
        "enabled": True,
        "backend": "github",
        "github": {
            "repository": "acme/public-nodes",
            "branch": "public",
            "path_prefix": "nodebench",
            "token_env": "GITHUB_TOKEN",
        },
    }
    payload.update(overrides)
    return PublishUploadConfig.model_validate(payload)


def test_github_backend_success(tmp_path: Path):
    publish_dir = whitelist_setup(tmp_path)
    opener, seen = make_recorder()
    uploader = GithubUploader(
        repository="acme/public-nodes",
        branch="public",
        path_prefix="nodebench",
        env={"GITHUB_TOKEN": "ghp_test_token"},
        opener=opener,
    )
    urls = uploader.upload_files(select_uploadable(publish_dir), RUN_ID)
    assert len(seen) == len(UPLOAD_WHITELIST)
    assert len(urls) == len(UPLOAD_WHITELIST)
    for request, url in zip(seen, urls):
        assert request.get_method() == "PUT"
        assert request.full_url.startswith(
            "https://api.github.com/repos/acme/public-nodes/contents/nodebench/"
        )
        body = json.loads(request.data.decode("utf-8"))
        assert RUN_ID in body["message"]
        assert body["branch"] == "public"
        name = request.full_url.rsplit("/", 1)[-1]
        raw = (publish_dir / name).read_bytes()
        assert base64.b64decode(body["content"]) == raw
        assert url.startswith("https://raw.githubusercontent.com/acme/public-nodes/public/nodebench/")
        assert request.get_header("Authorization") == "Bearer ghp_test_token"


def test_github_backend_http_failure(tmp_path: Path):
    publish_dir = whitelist_setup(tmp_path)
    opener, _seen = make_recorder(
        {"https://api.github.com/repos/acme/public-nodes/contents/nodebench/cf-addapi.txt": urllib.error.HTTPError(
            "https://api.github.com/x", 500, "error", {}, None
        )}
    )
    # only send one file so the scripted URL matches
    single = tmp_path / "one"
    single.mkdir()
    (single / CF_ADDAPI_NAME).write_text("1.1.1.1:443", encoding="utf-8")
    uploader = GithubUploader(
        repository="acme/public-nodes", env={"GITHUB_TOKEN": "t"}, opener=opener
    )
    with pytest.raises(PublishError) as info:
        uploader.upload_files([single / CF_ADDAPI_NAME], RUN_ID)
    assert info.value.code == "upload_failed"


def test_github_backend_permission_denied(tmp_path: Path):
    single = tmp_path / "one"
    single.mkdir()
    (single / CF_ADDAPI_NAME).write_text("1.1.1.1:443", encoding="utf-8")
    opener, _seen = make_recorder(
        {"https://api.github.com/repos/acme/public-nodes/contents/nodebench/cf-addapi.txt": urllib.error.HTTPError(
            "https://api.github.com/x", 403, "forbidden", {}, None
        )}
    )
    uploader = GithubUploader(
        repository="acme/public-nodes", env={"GITHUB_TOKEN": "t"}, opener=opener
    )
    with pytest.raises(PublishError) as info:
        uploader.upload_files([single / CF_ADDAPI_NAME], RUN_ID)
    assert info.value.code == "upload_permission_denied"


def test_github_backend_missing_token():
    with pytest.raises(ConfigError) as info:
        GithubUploader(repository="acme/public-nodes", env={})
    assert info.value.code == "upload_token_missing"


def test_github_prepares_all_payloads_before_any_request(tmp_path: Path):
    publish_dir = whitelist_setup(tmp_path)
    opener, seen = make_recorder()
    uploader = GithubUploader(
        repository="acme/public-nodes", env={"GITHUB_TOKEN": "t"}, opener=opener
    )
    with pytest.raises(PublishError) as info:
        uploader.upload_files(
            [publish_dir / CF_ADDAPI_NAME, publish_dir / PROXY_RAW_NAME], RUN_ID
        )
    assert info.value.code == "upload_not_allowed"
    assert seen == []


# ---------------------------------------------------------------------------
# http backend
# ---------------------------------------------------------------------------


def test_http_backend_put_success(tmp_path: Path):
    single = tmp_path / "one"
    single.mkdir()
    (single / CF_ADDAPI_NAME).write_text("1.1.1.1:443#edge", encoding="utf-8")
    opener, seen = make_recorder()
    uploader = HttpUploader(
        url="https://cdn.example.test/nodebench", method="PUT", opener=opener
    )
    urls = uploader.upload_files([single / CF_ADDAPI_NAME], RUN_ID)
    assert urls == ["https://cdn.example.test/nodebench/cf-addapi.txt"]
    assert len(seen) == 1
    assert seen[0].get_method() == "PUT"
    assert seen[0].data == b"1.1.1.1:443#edge"
    assert seen[0].get_header("X-nodebench-run") == RUN_ID


def test_http_backend_post_and_size_cap(tmp_path: Path):
    single = tmp_path / "one"
    single.mkdir()
    path = single / CF_ADDCSV_NAME
    path.write_bytes(b"x" * 50)
    opener, seen = make_recorder()
    uploader = HttpUploader(
        url="https://cdn.example.test/",
        method="POST",
        max_bytes=10,
        opener=opener,
    )
    with pytest.raises(PublishError) as info:
        uploader.upload_files([path], RUN_ID)
    assert info.value.code == "upload_too_large"
    assert seen == []

    uploader = HttpUploader(
        url="https://cdn.example.test/",
        method="POST",
        max_bytes=1024,
        opener=opener,
    )
    urls = uploader.upload_files([path], RUN_ID)
    assert urls == ["https://cdn.example.test/cf-addcsv.csv"]
    assert seen[0].get_method() == "POST"


def test_http_backend_rejects_private_hosts():
    with pytest.raises(ConfigError):
        HttpUploader(url="http://127.0.0.1/upload")
    with pytest.raises(ConfigError):
        HttpUploader(url="https://192.168.1.10/upload")
    with pytest.raises(ConfigError):
        HttpUploader(url="https://localhost/upload")


def test_http_backend_http_error(tmp_path: Path):
    single = tmp_path / "one"
    single.mkdir()
    (single / CF_ADDAPI_NAME).write_text("1.1.1.1:443", encoding="utf-8")
    opener, _seen = make_recorder(
        {"https://cdn.example.test/cf-addapi.txt": urllib.error.HTTPError(
            "https://cdn.example.test/cf-addapi.txt", 502, "bad", {}, None
        )}
    )
    uploader = HttpUploader(url="https://cdn.example.test", opener=opener)
    with pytest.raises(PublishError) as info:
        uploader.upload_files([single / CF_ADDAPI_NAME], RUN_ID)
    assert info.value.code == "upload_failed"


# ---------------------------------------------------------------------------
# factory + pipeline integration
# ---------------------------------------------------------------------------


def test_create_uploader_dispatch(tmp_path: Path):
    folder = create_uploader(
        PublishUploadConfig.model_validate(
            {"enabled": True, "backend": "folder", "folder_path": "out/public"}
        )
    )
    assert isinstance(folder, FolderUploader)
    github = create_uploader(
        github_config(), env={"GITHUB_TOKEN": "t"}
    )
    assert isinstance(github, GithubUploader)
    http = create_uploader(
        PublishUploadConfig.model_validate(
            {
                "enabled": True,
                "backend": "http",
                "http": {"url": "https://cdn.example.test/x", "method": "POST"},
            }
        )
    )
    assert isinstance(http, HttpUploader)
    assert http.method == "POST"
    bad = PublishUploadConfig.model_construct(
        enabled=True,
        backend="ftp",
        folder_path="",
        github=GithubUploadConfig(),
        http=HttpUploadConfig(),
    )
    with pytest.raises(ConfigError):
        create_uploader(bad)


def test_upload_disabled_returns_empty_without_touching_network(tmp_path: Path):
    publish_dir = whitelist_setup(tmp_path)
    config = PublishUploadConfig.model_validate(
        {"enabled": False, "backend": "folder", "folder_path": str(tmp_path / "mirror")}
    )
    urls = upload_published_files(config, publish_dir, base_name=RUN_ID)
    assert urls == []
    assert not (tmp_path / "mirror").exists()


MIT_LICENSES = [
    {"source_id": "local:sample-uri-list.txt", "license_tag": "mit"},
    {"source_id": "local:sample-endpoints.csv", "license_tag": "mit"},
]


def staged_outcome(output_dir: str | Path, report: dict) -> Path:
    """Build a real export under ``output_dir/<run_id>/export``."""
    from nodebench.exporters.build import build_export

    directory = Path(output_dir) / str(report["run_id"]) / "export"
    outcome = build_export(
        directory,
        report,
        nodes=[make_node()],
        edges=[make_edge()],
        proxies=[make_ranked_proxy()],
        endpoints=[make_ranked_endpoint()],
        scoring_version="1",
    )
    assert outcome.status == "ok"
    return directory


def make_score_report() -> ScoreReport:
    return ScoreReport(
        run_id=RUN_ID,
        runner_id="local:desktop-a",
        profile="local",
        scoring_version="1",
        proxies=[make_ranked_proxy()],
        endpoints=[make_ranked_endpoint()],
        counts={"ranked": 2, "filtered": 0, "pending": 0},
    )


def test_run_publish_stage_skips_upload_when_disabled(tmp_path: Path):
    config = load_config(DEFAULT_PATH, None, env={})
    config.output_dir = str(tmp_path / "out")
    config.publish.enabled = True
    config.publish.allow_proxy_credentials = True
    report = make_report(licenses=MIT_LICENSES)
    staged_outcome(config.output_dir, report)
    summary = run_publish_stage(
        config,
        report,
        outcome=load_export_outcome(config, RUN_ID),
        score_report=make_score_report(),
        licenses=MIT_LICENSES,
        allow_publish=True,
    )
    assert summary["status"] == "ok"
    assert summary["public_urls"] == []


def test_run_publish_stage_uploads_when_enabled(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_test_token")
    config = load_config(DEFAULT_PATH, None, env={})
    config.output_dir = str(tmp_path / "out")
    config.publish.enabled = True
    config.publish.allow_proxy_credentials = True
    config.publish.upload.enabled = True
    config.publish.upload.backend = "github"
    config.publish.upload.github.repository = "acme/public-nodes"
    report = make_report(licenses=MIT_LICENSES)
    staged_outcome(config.output_dir, report)
    opener, seen = make_recorder()
    monkeypatch.setattr("urllib.request.urlopen", opener)
    summary = run_publish_stage(
        config,
        report,
        outcome=load_export_outcome(config, RUN_ID),
        score_report=make_score_report(),
        licenses=MIT_LICENSES,
        allow_publish=True,
    )
    assert summary["status"] == "ok"
    assert len(summary["public_urls"]) == 4
    assert all(
        url.startswith("https://raw.githubusercontent.com/acme/public-nodes/")
        for url in summary["public_urls"]
    )
    assert len(seen) == 4
    names = {Path(request.full_url).name for request in seen}
    assert names == set(UPLOAD_WHITELIST)
    assert PROXY_RAW_NAME not in names


def test_run_publish_stage_marks_upload_failure(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_test_token")
    config = load_config(DEFAULT_PATH, None, env={})
    config.output_dir = str(tmp_path / "out")
    config.publish.enabled = True
    config.publish.allow_proxy_credentials = True
    config.publish.upload.enabled = True
    config.publish.upload.backend = "github"
    config.publish.upload.github.repository = "acme/public-nodes"
    report = make_report(licenses=MIT_LICENSES)
    staged_outcome(config.output_dir, report)

    def boom(request, timeout=None):
        raise urllib.error.URLError("network down")

    monkeypatch.setattr("urllib.request.urlopen", boom)
    summary = run_publish_stage(
        config,
        report,
        outcome=load_export_outcome(config, RUN_ID),
        score_report=make_score_report(),
        licenses=MIT_LICENSES,
        allow_publish=True,
    )
    assert summary["status"] == "failed"
    assert summary["reason"] == "upload_failed"
    assert summary["public_urls"] == []
    assert summary["errors"]


# ---------------------------------------------------------------------------
# CLI publish
# ---------------------------------------------------------------------------


def _stage_cli_run(tmp_path: Path) -> tuple[str, Path]:
    """Create run-report.json / scored.json / export/ for the CLI publish step."""
    output_dir = tmp_path / "out"
    run_dir = output_dir / RUN_ID
    report = make_report(licenses=MIT_LICENSES)
    staged_outcome(output_dir, report)
    (run_dir / "run-report.json").write_text(
        json.dumps(report), encoding="utf-8"
    )
    (run_dir / "scored.json").write_text(
        make_score_report().model_dump_json(indent=2), encoding="utf-8"
    )
    return RUN_ID, output_dir


def test_cli_publish_without_upload(monkeypatch, tmp_path: Path, capsys):
    run_id, output_dir = _stage_cli_run(tmp_path)
    monkeypatch.setenv(
        "NODEBENCH_PUBLISH__UPLOAD__ENABLED", "false"
    )
    code = main(["publish", "--run-id", run_id, "--output-dir", str(output_dir)])
    out = capsys.readouterr().out
    assert code == 0
    assert f"[{run_id}] publish status=ok" in out
    assert (output_dir / "latest" / MANIFEST_NAME).is_file()
    assert (output_dir / "latest" / CF_ADDAPI_NAME).is_file()
    assert "public_url=" not in out


def test_cli_publish_with_folder_upload(monkeypatch, tmp_path: Path, capsys):
    run_id, output_dir = _stage_cli_run(tmp_path)
    mirror = tmp_path / "mirror"
    monkeypatch.setenv("NODEBENCH_PUBLISH__UPLOAD__ENABLED", "true")
    monkeypatch.setenv("NODEBENCH_PUBLISH__UPLOAD__BACKEND", "folder")
    monkeypatch.setenv("NODEBENCH_PUBLISH__UPLOAD__FOLDER_PATH", str(mirror))
    code = main(["publish", "--run-id", run_id, "--output-dir", str(output_dir)])
    out = capsys.readouterr().out
    assert code == 0
    assert f"[{run_id}] publish status=ok" in out
    assert "public_url=" in out
    assert (mirror / CF_ADDAPI_NAME).is_file()
    assert (mirror / CF_ADDCSV_NAME).is_file()
    assert not (mirror / PROXY_RAW_NAME).exists()


def test_cli_publish_requires_run_id(tmp_path: Path, capsys):
    code = main(["publish"])
    err = capsys.readouterr().err
    assert code == 2
    assert "run-id" in err


def test_cli_publish_missing_export_exits_2(monkeypatch, tmp_path: Path, capsys):
    output_dir = tmp_path / "out"
    output_dir.mkdir()
    code = main(["publish", "--run-id", RUN_ID, "--output-dir", str(output_dir)])
    err = capsys.readouterr().err
    assert code == 2
    assert "export" in err.lower() or "artifact" in err.lower()


def test_cli_publish_blocked_exits_5(monkeypatch, tmp_path: Path, capsys):
    run_id, output_dir = _stage_cli_run(tmp_path)
    report_path = output_dir / run_id / "run-report.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    report["counts"] = dict(report["counts"], sources_total=1, sources_failed=1)
    report_path.write_text(json.dumps(report), encoding="utf-8")
    code = main(["publish", "--run-id", run_id, "--output-dir", str(output_dir)])
    err = capsys.readouterr().err
    assert code == 5
    assert "publish" in err.lower()


def test_publish_result_schema_has_public_urls():
    from nodebench.core.schema import PublishResult

    result = PublishResult(status="ok")
    assert result.public_urls == []
    dumped = result.model_dump()
    assert "public_urls" in dumped
    # backward compatible: older payloads without the field still validate
    legacy = PublishResult.model_validate({"status": "skipped", "reason": "disabled"})
    assert legacy.public_urls == []


def test_default_config_upload_disabled():
    config = load_config(DEFAULT_PATH, None, env={})
    assert config.publish.upload.enabled is False
    assert config.publish.upload.backend == "folder"
    assert config.publish.upload.github.branch == "public"
    assert config.publish.upload.github.token_env == "GITHUB_TOKEN"
    assert config.publish.upload.http.method == "PUT"
