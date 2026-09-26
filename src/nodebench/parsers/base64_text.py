from __future__ import annotations

import re

from nodebench.parsers.common import INVALID_BASE64, b64decode_flexible, make_issue

_SCHEME_LINE_RE = re.compile(r"(?m)^[ \t]*[A-Za-z][A-Za-z0-9+.\-]*://")


def decode_subscription(text, source_id=""):
    raw = "" if text is None else str(text)
    cleaned = raw.replace("﻿", "")
    if not cleaned.strip():
        return None, make_issue(
            source_id, INVALID_BASE64, "empty base64 subscription"
        )
    decoded = b64decode_flexible(cleaned)
    if decoded is None:
        return None, make_issue(source_id, INVALID_BASE64, "invalid base64 payload")
    try:
        inner = decoded.decode("utf-8")
    except UnicodeDecodeError:
        return None, make_issue(source_id, INVALID_BASE64, "invalid base64 payload")
    if not _SCHEME_LINE_RE.search(inner):
        return None, make_issue(
            source_id, INVALID_BASE64, "decoded payload has no uri lines"
        )
    return inner, None
