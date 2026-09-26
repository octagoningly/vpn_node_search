from __future__ import annotations

import re

from nodebench.core.schema import ParsedEndpoint, ParsedProxy
from nodebench.parsers.base64_text import decode_subscription
from nodebench.parsers.common import (
    PARSE_ERROR,
    UNSUPPORTED_CONTENT,
    make_issue,
)
from nodebench.parsers.csv import parse_endpoint_csv
from nodebench.parsers.uri import parse_uri
from nodebench.parsers.yaml_clash import parse_clash_yaml

_SCHEME_DETECT = re.compile(
    r"^[ \t]*[A-Za-z][A-Za-z0-9+.\-]*://", re.IGNORECASE
)

def _uri_text_lines(text, content_type, source_id, proxies, issues):
    scheme_lines = []
    for line_no, raw in enumerate(text.splitlines(), start=1):
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if _SCHEME_DETECT.match(stripped) is None:
            if content_type == "uri_list":
                issues.append(
                    make_issue(
                        source_id,
                        UNSUPPORTED_CONTENT,
                        "line is not a supported uri",
                        "line:{0}".format(line_no),
                    )
                )
            continue
        scheme_lines.append((line_no, stripped))
    if content_type == "text" and not scheme_lines:
        issues.append(
            make_issue(
                source_id,
                UNSUPPORTED_CONTENT,
                "payload has no uri lines",
            )
        )
        return
    for line_no, stripped in scheme_lines:
        node, issue = parse_uri(stripped, source_id)
        if node is not None:
            proxies.append(node)
        if issue is not None:
            if not issue.raw_ref:
                issue.raw_ref = "line:{0}".format(line_no)
            issues.append(issue)


def parse_raw_item(item):
    proxies: list[ParsedProxy] = []
    endpoints: list[ParsedEndpoint] = []
    issues = []
    content = item.content_type
    source_id = item.source_id
    try:
        if content in ("uri_list", "base64_sub", "text"):
            text = item.payload
            if content == "base64_sub":
                decoded, decode_issue = decode_subscription(item.payload, source_id)
                if decode_issue is not None:
                    issues.append(decode_issue)
                    text = ""
                else:
                    text = decoded
            if text:
                _uri_text_lines(text, content, source_id, proxies, issues)
        elif content == "yaml":
            yaml_proxies, yaml_issues = parse_clash_yaml(item.payload, source_id)
            proxies.extend(yaml_proxies)
            issues.extend(yaml_issues)
        elif content == "csv":
            csv_endpoints, csv_issues = parse_endpoint_csv(item.payload, source_id)
            endpoints.extend(csv_endpoints)
            issues.extend(csv_issues)
        else:
            issues.append(
                make_issue(
                    source_id,
                    UNSUPPORTED_CONTENT,
                    "unsupported content type",
                )
            )
    except Exception as exc:
        issues.append(
            make_issue(source_id, PARSE_ERROR, type(exc).__name__, "")
        )
    for issue in issues:
        if not issue.source_id:
            issue.source_id = source_id
    return proxies, endpoints, issues
