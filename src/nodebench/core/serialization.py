from __future__ import annotations

import json
import os
import tempfile
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel

FORBIDDEN_KEY_EXACT = frozenset({"payload", "secrets", "secret"})
FORBIDDEN_KEY_TOKENS = ("secret", "token", "password", "key")


def is_forbidden_key(key: str) -> bool:
    lowered = key.lower()
    if lowered in FORBIDDEN_KEY_EXACT:
        return True
    return any(token in lowered for token in FORBIDDEN_KEY_TOKENS)


def _strip_credentials(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return _strip_credentials(value.model_dump(mode="python"))
    if isinstance(value, dict):
        return {
            key: _strip_credentials(item)
            for key, item in value.items()
            if not is_forbidden_key(str(key))
        }
    if isinstance(value, (list, tuple)):
        return [_strip_credentials(item) for item in value]
    return value


def public_dump(model_or_dict: Any) -> dict:
    """Recursively dump a model or mapping without credential-bearing keys."""
    if isinstance(model_or_dict, BaseModel):
        data: Any = model_or_dict.model_dump(mode="python")
    elif isinstance(model_or_dict, dict):
        data = model_or_dict
    else:
        raise TypeError(
            f"public_dump expects a pydantic model or dict, got {type(model_or_dict)!r}"
        )
    stripped = _strip_credentials(data)
    if not isinstance(stripped, dict):
        raise TypeError("public_dump result must be a mapping")
    return stripped


def _encode_datetime(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    text = value.astimezone(timezone.utc).isoformat()
    if text.endswith("+00:00"):
        text = text[: -len("+00:00")] + "Z"
    return text


def _json_default(value: Any) -> Any:
    if isinstance(value, datetime):
        return _encode_datetime(value)
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, BaseModel):
        return value.model_dump(mode="python")
    if isinstance(value, (set, frozenset)):
        return sorted(value, key=repr)
    raise TypeError(f"object of type {type(value).__name__} is not JSON serializable")


def dumps_json(obj: Any) -> str:
    """Serialize to indented UTF-8 JSON; datetimes become UTC ISO 8601 with Z.

    Missing values stay ``null`` and are never coerced to ``0``. Secret
    stripping belongs to :func:`public_dump`; run it first for artifacts that
    leave the local machine.
    """
    return json.dumps(
        obj,
        ensure_ascii=False,
        indent=2,
        default=_json_default,
        allow_nan=False,
    )


def write_json_atomic(path: str | os.PathLike[str], obj: Any) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = dumps_json(obj).encode("utf-8")
    fd, tmp_name = tempfile.mkstemp(
        dir=str(target.parent),
        prefix=target.name + ".",
        suffix=".tmp",
    )
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, target)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    return target


__all__ = [
    "public_dump",
    "dumps_json",
    "write_json_atomic",
    "is_forbidden_key",
]
