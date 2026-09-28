from __future__ import annotations

import re
from typing import Any, Iterable, Mapping, Sequence

CF_ADDAPI_NAME = "cf-addapi.txt"

# v2rayN-visible score summary: speed(MB/s)-purity-stability-country
# e.g. `104.17.29.227:8443#4.6-0.85-0.72-SG`
DEFAULT_ADDAPI_REMARK_TEMPLATE = "speed-purity-stability-country"
UNKNOWN_SCORE_PLACEHOLDER = "--"
UNKNOWN_COUNTRY_PLACEHOLDER = "??"
REMARK_FIELD_NAMES = ("speed", "purity", "stability", "country")

_UNKNOWN_COUNTRY_CODES = frozenset(
    {"", "UNKNOWN", "UNK", "NONE", "N/A", "ZZ", "??"}
)


def _host(address: str) -> str:
    text = str(address or "").strip()
    return "[{0}]".format(text) if ":" in text else text


def _remark(value) -> str:
    return str(value or "").replace("#", "").strip()


def format_speed(speed_mb_s: Any) -> str:
    """One-decimal MB/s figure; unmeasured ranks use ``0.0`` (ranked always has speed)."""
    if speed_mb_s is None:
        return "0.0"
    try:
        number = float(speed_mb_s)
    except (TypeError, ValueError):
        return "0.0"
    if number != number:
        return "0.0"
    return "{0:.1f}".format(number)


def format_score(value: Any, *, digits: int = 2, scale_01: bool = False) -> str:
    """Two-decimal 0-1 score; unknown becomes ``--``.

    ``scale_01`` accepts provider 0-100 scores and normalizes them to 0-1.
    """
    if value is None:
        return UNKNOWN_SCORE_PLACEHOLDER
    try:
        number = float(value)
    except (TypeError, ValueError):
        return UNKNOWN_SCORE_PLACEHOLDER
    if number != number:
        return UNKNOWN_SCORE_PLACEHOLDER
    if scale_01 and number > 1.0:
        number = number / 100.0
    return "{0:.{1}f}".format(number, digits)


def format_country(country_code: Any) -> str:
    """ISO-like two-letter code; unknown becomes ``??``."""
    text = str(country_code or "").strip().upper()
    if text in _UNKNOWN_COUNTRY_CODES:
        return UNKNOWN_COUNTRY_PLACEHOLDER
    return text


def score_purity(ranked: Any) -> float | None:
    """Purity from score_breakdown['purity'] else 1 - risk/100."""
    breakdown = getattr(ranked, "score_breakdown", None)
    if isinstance(breakdown, Mapping):
        value = breakdown.get("purity")
        if value is not None:
            try:
                return float(value)
            except (TypeError, ValueError):
                pass
    risk = getattr(ranked, "risk", None)
    if risk is None:
        return None
    try:
        return 1.0 - float(risk) / 100.0
    except (TypeError, ValueError):
        return None


def score_stability(ranked: Any) -> float | None:
    """Stability from score_breakdown['stability'] else availability_rate."""
    breakdown = getattr(ranked, "score_breakdown", None)
    if isinstance(breakdown, Mapping):
        value = breakdown.get("stability")
        if value is not None:
            try:
                return float(value)
            except (TypeError, ValueError):
                pass
    rate = getattr(ranked, "availability_rate", None)
    if rate is None:
        return None
    try:
        return float(rate)
    except (TypeError, ValueError):
        return None


def format_addapi_remark(
    *,
    speed_mb_s: Any = None,
    purity: Any = None,
    stability: Any = None,
    country_code: Any = None,
    template: str | None = None,
) -> str:
    """Render the score-summary remark shown by v2rayN after ``#``.

    The default template ``speed-purity-stability-country`` yields e.g.
    ``4.6-0.85-0.72-SG``. Custom templates accept ``{speed}`` ``{purity}``
    ``{stability}`` ``{country}`` placeholders or the bare field names.
    """
    values = {
        "speed": format_speed(speed_mb_s),
        "purity": format_score(purity, digits=2, scale_01=True),
        "stability": format_score(stability, digits=2, scale_01=True),
        "country": format_country(country_code),
    }
    text = str(template if template is not None else DEFAULT_ADDAPI_REMARK_TEMPLATE)
    if not text.strip():
        return ""
    for key in REMARK_FIELD_NAMES:
        text = text.replace("{" + key + "}", values[key])
    for key in REMARK_FIELD_NAMES:
        text = re.sub(r"\b{0}\b".format(key), values[key], text)
    return _remark(text)


def addapi_remark_from_ranked(
    ranked: Any,
    *,
    template: str | None = None,
    fallback: str = "",
) -> str:
    """Build the ADDAPI remark for one RankedEndpoint-like object."""
    remark = format_addapi_remark(
        speed_mb_s=getattr(ranked, "speed_mb_s", None),
        purity=score_purity(ranked),
        stability=score_stability(ranked),
        country_code=getattr(ranked, "country_code", None),
        template=template,
    )
    if remark:
        return remark
    return _remark(fallback)


def build_addapi(rows: Iterable[Sequence[object]]) -> str:
    lines = []
    for row in rows:
        address, port, remark = row[0], int(row[1]), _remark(row[2] if len(row) > 2 else "")
        line = "{0}:{1}".format(_host(address), int(port))
        if remark:
            line = "{0}#{1}".format(line, remark)
        lines.append(line)
    if not lines:
        return ""
    return "".join("{0}\n".format(line) for line in lines)
