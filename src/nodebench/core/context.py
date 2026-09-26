from __future__ import annotations

import re
import sys
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Iterable, Mapping, TextIO

from nodebench.core.schema import RunContext

if TYPE_CHECKING:
    from nodebench.core.config import AppConfig

UUID_PATTERN = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
)
PARAM_PATTERN = re.compile(
    r"(?i)\b(password|passwd|pwd|token|secret|key|auth|uuid)\s*=\s*[^&\s\"'<>]+"
)
URL_CREDENTIAL_PATTERN = re.compile(r"(?i)\b([a-z][a-z0-9+.\-]*://)([^\s/@:]+):([^\s/@]+)@")

MASK = "***"
MIN_SECRET_LENGTH = 4

_SECRET_LITERALS: set[str] = set()


def register_secrets(values: Iterable[str] | None) -> None:
    if not values:
        return
    for value in values:
        if isinstance(value, str) and len(value) >= MIN_SECRET_LENGTH:
            _SECRET_LITERALS.add(value)


def clear_registered_secrets() -> None:
    _SECRET_LITERALS.clear()


def registered_secrets() -> frozenset[str]:
    return frozenset(_SECRET_LITERALS)


def redact(text: str, secrets: Iterable[str] | None = None) -> str:
    """Mask UUIDs, credential parameters, URL user info and known literals."""
    if text is None:
        return ""
    result = str(text)
    candidates: list[str] = []
    if secrets:
        candidates.extend(str(item) for item in secrets if item is not None)
    candidates.extend(_SECRET_LITERALS)
    for literal in sorted({item for item in candidates if len(item) >= MIN_SECRET_LENGTH}, key=len, reverse=True):
        if literal:
            result = result.replace(literal, MASK)
    result = URL_CREDENTIAL_PATTERN.sub(rf"\1{MASK}@", result)
    result = PARAM_PATTERN.sub(rf"\1={MASK}", result)
    result = UUID_PATTERN.sub(MASK, result)
    return result


def _coerce_args(cli_args: Any) -> dict[str, Any]:
    if cli_args is None:
        return {}
    if isinstance(cli_args, Mapping):
        return {str(key): value for key, value in cli_args.items()}
    return {
        key: value
        for key, value in vars(cli_args).items()
        if not key.startswith("_") and value is not None
    }


def _coerce_budget(value: Any) -> dict[str, float]:
    if not value:
        return {}
    if not isinstance(value, Mapping):
        raise ValueError(f"budget must be a mapping of names to numbers, got {type(value)!r}")
    budget: dict[str, float] = {}
    for key, item in value.items():
        budget[str(key)] = float(item)
    return budget


def _coerce_deadline(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def build_run_context(config: AppConfig, cli_args: Any = None) -> RunContext:
    """Build a :class:`RunContext` from merged config plus CLI arguments."""
    args = _coerce_args(cli_args)
    profile = str(args.get("profile") or config.profile)
    runner_id = str(args.get("runner_id") or config.runner_id)
    budget = dict(config.budget)
    budget.update(_coerce_budget(args.get("budget")))
    deadline = _coerce_deadline(args.get("deadline"))
    run_id = args.get("run_id")
    if run_id:
        return RunContext(
            run_id=str(run_id),
            runner_id=runner_id,
            profile=profile,
            budget=budget,
            deadline=deadline,
        )
    return RunContext.create(
        profile=profile,
        runner_id=runner_id,
        budget=budget,
        deadline=deadline,
    )


class RunLogger:
    def __init__(self, run_id: str, source_id: str | None = None, stream: TextIO | None = None) -> None:
        self.run_id = run_id
        self.source_id = source_id
        self.stream = stream or sys.stdout

    def _prefix(self) -> str:
        if self.source_id:
            return f"[{self.run_id}][{self.source_id}]"
        return f"[{self.run_id}]"

    def log(self, message: Any) -> str:
        line = f"{self._prefix()} {redact(str(message))}"
        print(line, file=self.stream)
        return line

    def info(self, message: Any) -> str:
        return self.log(message)

    def warning(self, message: Any) -> str:
        return self.log(message)

    def warn(self, message: Any) -> str:
        return self.log(message)

    def error(self, message: Any) -> str:
        return self.log(message)

    def debug(self, message: Any) -> str:
        return self.log(message)


def get_logger(run_id: str, source_id: str | None = None, stream: TextIO | None = None) -> RunLogger:
    return RunLogger(run_id=run_id, source_id=source_id, stream=stream)


__all__ = [
    "redact",
    "register_secrets",
    "clear_registered_secrets",
    "registered_secrets",
    "build_run_context",
    "get_logger",
    "RunLogger",
]
