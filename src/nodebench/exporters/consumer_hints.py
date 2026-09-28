from __future__ import annotations

from typing import Any, Mapping

from nodebench.exporters.cf_addapi import (
    CF_ADDAPI_NAME,
    DEFAULT_ADDAPI_REMARK_TEMPLATE,
)
from nodebench.exporters.cf_addcsv import CF_ADDCSV_NAME

# WorkerVless2sub / edgetunnel consumer notes. Endpoints alone cannot form a
# usable VLESS subscription: the operator must supply their own Host/UUID/
# Path/SNI. This project never fabricates a subscription from edge addresses.
USER_REQUIRED_SUBSCRIPTION_FIELDS = ("host", "uuid", "path", "sni")

ADDAPI_LINE_HELP = (
    "one endpoint per line: HOST:PORT#speed-purity-stability-country or "
    "[IPv6]:PORT#speed-purity-stability-country"
)
ADDCSV_COLUMN_HELP = (
    "iptest-style nine columns in fixed order: "
    "IP地址,端口,回源端口,TLS,数据中心,地区,城市,TCP延迟(ms),速度(MB/s)"
)
DLS_NOTE = (
    "WorkerVless2sub DLS keeps rows whose speed column numeric value is "
    "greater than or equal to the threshold; the unit is ignored by DLS, "
    "so keep the CSV speed column in MB/s and set DLS to a number in that scale."
)


def build_consumer_hints(
    *,
    dls_min_speed_mb_s: float | None = None,
    endpoint_count: int = 0,
    measured_endpoint_count: int = 0,
    addapi_remark_template: str | None = None,
) -> dict[str, Any]:
    """Describe how to feed the exported files to WorkerVless2sub.

    The hints deliberately contain no credentials and no ready-made
    subscription URL: Host/UUID/Path/SNI stay operator-supplied.
    """
    template = addapi_remark_template or DEFAULT_ADDAPI_REMARK_TEMPLATE
    dls: dict[str, Any] = {
        "field": "速度(MB/s)",
        "unit": "MB/s",
        "note": DLS_NOTE,
    }
    if dls_min_speed_mb_s is not None:
        dls["suggested_min"] = float(dls_min_speed_mb_s)
        dls["suggested_from"] = "scoring.filters.min_speed_mb_s"
    return {
        "target": "cmliu/WorkerVless2sub",
        "files": {
            "addapi": {
                "path": CF_ADDAPI_NAME,
                "relative_to": "export directory",
                "env": "ADDAPI",
                "format": ADDAPI_LINE_HELP,
                "remark_template": template,
                "remark_example": "104.17.29.227:8443#4.6-0.85-0.72-SG",
            },
            "addcsv": {
                "path": CF_ADDCSV_NAME,
                "relative_to": "export directory",
                "env": "ADDCSV",
                "format": ADDCSV_COLUMN_HELP,
            },
        },
        "addapi_remark": {
            "template": template,
            "separator": "-",
            "fields": {
                "speed": "download speed in MB/s, one decimal (unit not written in the remark)",
                "purity": "0-1 score, two decimals; -- when unknown",
                "stability": "0-1 score, two decimals; -- when unknown",
                "country": "ISO two-letter code; ?? when unknown",
            },
            "speed_unit": "MB/s",
        },
        "dls": dls,
        "subscription": {
            "user_required_fields": list(USER_REQUIRED_SUBSCRIPTION_FIELDS),
            "note": (
                "Edge endpoints are not VLESS nodes by themselves. Configure "
                "your own Host/UUID/Path/SNI (and SNI defaulting to host) on "
                "WorkerVless2sub or your edgetunnel site before generating a "
                "subscription. This export does not generate a fake subscription."
            ),
            "generates_fake_subscription": False,
        },
        "counts": {
            "endpoints": int(endpoint_count),
            "measured_this_run": int(measured_endpoint_count),
        },
        "historical_values_note": (
            "latency/speed columns in cf-addcsv.csv are this-run measurements "
            "when present. Imported historical values stay in item params "
            "(historical_latency_ms / historical_speed_mb_s) and are not "
            "presented as fresh measurements."
        ),
    }


def hints_from_mapping(hints: Mapping[str, Any] | None) -> dict[str, Any]:
    if not hints:
        return build_consumer_hints()
    return dict(hints)
