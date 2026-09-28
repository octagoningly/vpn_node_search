from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from nodebench.core.config import load_config

PROJECT_ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = PROJECT_ROOT / ".github" / "workflows"
CONFIG_DIR = PROJECT_ROOT / "config"
DEFAULT_PATH = CONFIG_DIR / "default.yaml"
PROFILE_PATH = CONFIG_DIR / "profiles" / "actions-collect.yaml"

WORKFLOW_FILES = ["collect.yml", "daily.yml", "offline.yml"]


def load_workflow(name: str) -> dict:
    text = (WORKFLOWS / name).read_text(encoding="utf-8")
    data = yaml.safe_load(text)
    assert isinstance(data, dict), f"{name} root must be a mapping"
    return data


@pytest.mark.parametrize("name", WORKFLOW_FILES)
def test_workflow_yaml_parses(name: str):
    data = load_workflow(name)
    assert "jobs" in data
    assert isinstance(data["jobs"], dict) and data["jobs"]


@pytest.mark.parametrize("name", WORKFLOW_FILES)
def test_workflow_has_no_inline_secrets(name: str):
    text = (WORKFLOWS / name).read_text(encoding="utf-8")
    assert "ghp_" not in text
    assert "github_pat_" not in text
    # tokens must come from secrets context, never literal values
    assert "password:" not in text.lower()


def test_collect_workflow_uses_actions_collect_profile():
    data = load_workflow("collect.yml")
    steps = data["jobs"]["collect"]["steps"]
    run_steps = [s for s in steps if "run" in s]
    joined = "\n".join(str(s.get("run", "")) for s in run_steps)
    assert "--profile actions-collect" in joined
    assert "--no-publish" in joined


def test_collect_workflow_injects_github_token():
    data = load_workflow("collect.yml")
    steps = data["jobs"]["collect"]["steps"]
    token_steps = [
        s for s in steps if isinstance(s.get("env"), dict) and "GITHUB_TOKEN" in s["env"]
    ]
    assert token_steps, "collect job must inject GITHUB_TOKEN for sources.github"
    for step in token_steps:
        assert step["env"]["GITHUB_TOKEN"] == "${{ secrets.GITHUB_TOKEN }}"
        run = str(step.get("run", ""))
        # never echo / print the token value
        assert "echo" not in run.lower() or "${{ secrets.GITHUB_TOKEN }}" not in run


def test_collect_workflow_uploads_redacted_artifacts():
    data = load_workflow("collect.yml")
    steps = data["jobs"]["collect"]["steps"]
    upload = [
        s
        for s in steps
        if str(s.get("uses", "")).startswith("actions/upload-artifact")
    ]
    assert upload, "collect job must upload a candidate artifact"
    stage = next(
        s for s in steps if "Stage redacted candidate lists" in str(s.get("name", ""))
    )
    stage_run = str(stage.get("run", ""))
    for banned in ("proxy-raw.txt", "cf-addapi.txt", "*.db", "*.env"):
        assert banned in stage_run, f"staging must scrub {banned}"


def test_collect_workflow_supports_dispatch_inputs():
    data = load_workflow("collect.yml")
    trigger = data.get("on") or data.get(True)  # PyYAML parses `on:` as True
    assert isinstance(trigger, dict)
    dispatch = trigger.get("workflow_dispatch")
    assert isinstance(dispatch, dict)
    inputs = dispatch.get("inputs") or {}
    assert "queries" in inputs
    assert "max_files" in inputs


def test_actions_collect_profile_loads():
    config = load_config(DEFAULT_PATH, PROFILE_PATH, env={})
    assert config.profile == "actions-collect"
    assert config.sources.github.enabled is True
    assert config.sources.github.queries
    assert config.sources.github.allowed_repositories
    assert config.sources.github.offline is False
    # proxy probing off; CF probe off by default (Actions 网络≠用户网络)
    assert config.probe.proxy.enabled is False
    assert config.probe.cf.enabled is False
    # publish stays off on Actions
    assert config.publish.enabled is False
    assert config.publish.allow_proxy_credentials is False


def test_actions_collect_profile_targets_cf_addapi_queries():
    config = load_config(DEFAULT_PATH, PROFILE_PATH, env={})
    joined = " ".join(config.sources.github.queries).lower()
    assert "addressesapi" in joined or "cf-addapi" in joined
    assert "filename:" in joined
