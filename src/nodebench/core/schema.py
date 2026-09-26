from __future__ import annotations

import secrets
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

SCHEMA_VERSION = 1
FINGERPRINT_VERSION = 1

RUN_ID_PATTERN = r"^\d{8}T\d{6}Z-[0-9a-f]{6}$"


class Status(str, Enum):
    OK = "ok"
    FAILED = "failed"
    SKIPPED = "skipped"
    UNSUPPORTED = "unsupported"
    UNKNOWN = "unknown"
    STALE = "stale"


class Kind(str, Enum):
    PROXY_NODE = "proxy_node"
    EDGE_ENDPOINT = "edge_endpoint"


class ErrorInfo(BaseModel):
    model_config = ConfigDict(extra="forbid")

    stage: str
    code: str
    message_redacted: str
    retryable: bool = False


class RunContext(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str = Field(pattern=RUN_ID_PATTERN)
    runner_id: str
    profile: str
    budget: dict[str, float] = Field(default_factory=dict)
    deadline: datetime | None = None

    @classmethod
    def create(
        cls,
        profile: str,
        runner_id: str,
        budget: dict[str, float] | None = None,
        deadline: datetime | None = None,
        now: datetime | None = None,
    ) -> RunContext:
        moment = now or datetime.now(timezone.utc)
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        moment = moment.astimezone(timezone.utc)
        stamp = moment.strftime("%Y%m%dT%H%M%SZ")
        run_id = f"{stamp}-{secrets.token_hex(3)}"
        return cls(
            run_id=run_id,
            runner_id=runner_id,
            profile=profile,
            budget=dict(budget or {}),
            deadline=deadline,
        )


class RawItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_id: str
    content_type: Literal["uri_list", "base64_sub", "yaml", "csv", "text"]
    payload: str
    fetched_at: datetime
    license_tag: str = "unknown"
    source_ref: str = ""
    item_id: str | None = None


class ParseIssue(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_id: str
    code: str
    message_redacted: str
    raw_ref: str = ""


class ParsedProxy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_id: str
    protocol: str
    server: str
    port: int
    transport: str = "tcp"
    security: str = "none"
    params: dict[str, Any] = Field(default_factory=dict)
    secrets: dict[str, str] = Field(default_factory=dict, repr=False)
    remarks: str = ""


class ParsedEndpoint(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_id: str
    address: str
    port: int
    target_host: str = ""
    tls: bool = False
    params: dict[str, Any] = Field(default_factory=dict)
    remarks: str = ""


class ProxyNode(BaseModel):
    model_config = ConfigDict(extra="forbid")

    item_id: str
    kind: Literal["proxy_node"]
    fingerprint: str
    fingerprint_version: int
    protocol: str
    server: str
    port: int
    transport: str
    security: str
    params: dict[str, Any] = Field(default_factory=dict)
    secrets: dict[str, str] = Field(default_factory=dict, repr=False)
    remarks: str = ""
    source_ids: list[str] = Field(default_factory=list)
    raw_refs: list[str] = Field(default_factory=list)


class EdgeEndpoint(BaseModel):
    model_config = ConfigDict(extra="forbid")

    item_id: str
    kind: Literal["edge_endpoint"]
    fingerprint: str
    fingerprint_version: int
    address: str
    port: int
    target_host: str = ""
    tls: bool = False
    params: dict[str, Any] = Field(default_factory=dict)
    remarks: str = ""
    source_ids: list[str] = Field(default_factory=list)
    raw_refs: list[str] = Field(default_factory=list)


class SourceReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_id: str
    ok: bool
    fetched: int = 0
    errors: list[ErrorInfo] = Field(default_factory=list)
    scope: str = ""
    redacted: bool = True


__all__ = [
    "SCHEMA_VERSION",
    "FINGERPRINT_VERSION",
    "RUN_ID_PATTERN",
    "Status",
    "Kind",
    "ErrorInfo",
    "RunContext",
    "RawItem",
    "ParseIssue",
    "ParsedProxy",
    "ParsedEndpoint",
    "ProxyNode",
    "EdgeEndpoint",
    "SourceReport",
]
