from __future__ import annotations

from typing import Any, Mapping

from nodebench.core.serialization import dumps_json

REPORT_NAME = "report.json"


def report_bytes(report: Mapping[str, Any]) -> bytes:
    return dumps_json(dict(report)).encode("utf-8")
