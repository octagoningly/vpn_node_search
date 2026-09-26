from __future__ import annotations

import base64
import binascii
import re
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from nodebench.core.schema import ErrorInfo, RawItem, SourceReport

MAX_SOURCE_FILE_BYTES = 4 * 1024 * 1024
MAX_SOURCE_FILES = 500
MIN_BASE64_LENGTH = 16

BOM = "\ufeff"
SCHEME_PATTERN = re.compile(r"(?im)^[ \t]*[a-z][a-z0-9+.\-]*://")
BASE64_PATTERN = re.compile(r"^[A-Za-z0-9+/]+={0,2}$")
YAML_SUFFIXES = frozenset({".yaml", ".yml"})
BASE64_SUFFIXES = frozenset({".b64", ".base64"})


class CollectOutcome(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[RawItem] = Field(default_factory=list)
    reports: list[SourceReport] = Field(default_factory=list)


def make_error(code: str, message: str, retryable: bool = False) -> ErrorInfo:
    return ErrorInfo(
        stage="collect",
        code=code,
        message_redacted=message,
        retryable=retryable,
    )


def _decode_base64(compact: str) -> str | None:
    if len(compact) % 4 == 1:
        return None
    padded = compact + "=" * (-len(compact) % 4)
    try:
        decoded = base64.b64decode(padded, validate=True)
    except (binascii.Error, ValueError):
        return None
    try:
        return decoded.decode("utf-8")
    except UnicodeDecodeError:
        return None


def looks_like_base64(text: str) -> bool:
    compact = "".join(text.split())
    if len(compact) < MIN_BASE64_LENGTH or not BASE64_PATTERN.fullmatch(compact):
        return False
    decoded = _decode_base64(compact)
    if decoded is None:
        return False
    return "://" in decoded or "\n" in decoded


def detect_content_type(text: str) -> str:
    stripped = text.strip()
    if not stripped:
        return "text"
    if SCHEME_PATTERN.search(stripped):
        return "uri_list"
    if looks_like_base64(stripped):
        return "base64_sub"
    return "text"


def content_type_for(path: Path, text: str) -> str:
    suffix = path.suffix.lower()
    if suffix == ".csv":
        return "csv"
    if suffix in YAML_SUFFIXES:
        return "yaml"
    if suffix in BASE64_SUFFIXES:
        return "base64_sub"
    return detect_content_type(text)


__all__ = [
    "MAX_SOURCE_FILE_BYTES",
    "MAX_SOURCE_FILES",
    "MIN_BASE64_LENGTH",
    "BOM",
    "CollectOutcome",
    "make_error",
    "looks_like_base64",
    "detect_content_type",
    "content_type_for",
]
