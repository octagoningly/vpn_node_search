from __future__ import annotations

import io
import re

from pydantic import ValidationError

from nodebench.core.config import AppConfig
from nodebench.core.context import (
    build_run_context,
    clear_registered_secrets,
    get_logger,
    redact,
    register_secrets,
)
from nodebench.core.schema import RUN_ID_PATTERN


def make_config() -> AppConfig:
    return AppConfig(
        profile="local",
        runner_id="local:desktop-a",
        budget={"total_mb": 512.0, "max_nodes": 25.0},
    )


def test_build_run_context_generates_run_id():
    config = make_config()
    context = build_run_context(config)
    assert re.fullmatch(RUN_ID_PATTERN, context.run_id)
    assert context.profile == "local"
    assert context.runner_id == "local:desktop-a"
    assert context.budget == {"total_mb": 512.0, "max_nodes": 25.0}


def test_build_run_context_applies_cli_overrides():
    config = make_config()
    context = build_run_context(
        config,
        {
            "profile": "github",
            "runner_id": "actions:ubuntu-24",
            "budget": {"max_nodes": 5},
        },
    )
    assert context.profile == "github"
    assert context.runner_id == "actions:ubuntu-24"
    assert context.budget == {"total_mb": 512.0, "max_nodes": 5.0}


def test_build_run_context_respects_explicit_run_id():
    config = make_config()
    context = build_run_context(config, {"run_id": "20260926T033700Z-abc123"})
    assert context.run_id == "20260926T033700Z-abc123"


def test_build_run_context_rejects_invalid_run_id():
    config = make_config()
    try:
        build_run_context(config, {"run_id": "bad-run-id"})
    except ValidationError:
        return
    raise AssertionError("invalid run id must not be accepted")


def test_redact_masks_credential_parameters():
    text = redact(
        "connect token=abc12345&password=hunter2&uuid=123e4567-e89b-12d3-a456-426614174000"
    )
    assert "abc12345" not in text
    assert "hunter2" not in text
    assert "123e4567-e89b-12d3-a456-426614174000" not in text
    assert "token=***" in text


def test_redact_masks_url_credentials():
    text = redact("fetch failed for https://user:pass@subs.example.test/list")
    assert "user:pass@" not in text
    assert "https://***@subs.example.test/list" in text


def test_redact_masks_registered_secret_literals():
    register_secrets(["ghp_example_token_value"])
    try:
        text = redact("lookup ghp_example_token_value failed")
        assert "ghp_example_token_value" not in text
        assert "***" in text
    finally:
        clear_registered_secrets()


def test_redact_leaves_plain_text_untouched():
    assert redact("203.0.113.7 latency 125 ms") == "203.0.113.7 latency 125 ms"


def test_logger_prefix_and_redaction():
    stream = io.StringIO()
    logger = get_logger("20260926T033700Z-abc123", source_id="local-1", stream=stream)
    logger.info("token=abc12345")
    line = stream.getvalue()
    assert line.startswith("[20260926T033700Z-abc123][local-1] ")
    assert "abc12345" not in line
    assert "token=***" in line
