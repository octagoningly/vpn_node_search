"""Protocol support level annotation.

判定规则（开发规则 §1 / §3.3 / §3.4）：

- ``parse_only``：该协议仅能被解析、规范化与去重，尚未通过真实连通测试
  验证「支持检测」。所有解析结果默认落在这一级。
- ``probe_supported``：该协议已通过真实连通测试，才允许标为「支持检测」。
  证据必须是 ``probe_mode=real`` 且 ``status=ok`` 的代理探测结果；入口
  TCP 可达、模拟（``simulated``）、未执行（``not_run``）或跳过结果均不
  构成证据。

解析阶段的 ``protocol_support`` 一律为 ``parse_only``；运行报告按当次
探测证据逐协议提升级别，并把规则与级别写入 ``report.json`` 的
``protocol_support`` 节点，供导出与下游消费者核对。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from nodebench.core.schema import ProbeMode, ProbeStatus, ProtocolSupport

SUPPORT_RULE = (
    "probe_supported requires a real connectivity test "
    "(probe_mode=real and status=ok); otherwise parse_only"
)

SUPPORT_LEVELS = (
    ProtocolSupport.PARSE_ONLY.value,
    ProtocolSupport.PROBE_SUPPORTED.value,
)


def parse_support_level() -> ProtocolSupport:
    """Level assigned at parse/normalize time: always ``parse_only``."""
    return ProtocolSupport.PARSE_ONLY


def _status_value(value: Any) -> str:
    return str(getattr(value, "value", value))


def is_real_probe_ok(result: Any) -> bool:
    """True only for a real-mode probe result with status=ok."""
    if isinstance(result, Mapping):
        mode = result.get("probe_mode")
        status = result.get("status")
    else:
        mode = getattr(result, "probe_mode", None)
        status = getattr(result, "status", None)
    if _status_value(mode) != ProbeMode.REAL.value:
        return False
    return _status_value(status) == ProbeStatus.OK.value


def protocol_support_levels(
    nodes: Iterable[Any],
    probe_results: Iterable[Any] | None = None,
) -> dict[str, str]:
    """Map each protocol seen in ``nodes`` to its honest support level.

    A protocol is raised to ``probe_supported`` only when at least one of its
    nodes has a real successful probe in ``probe_results``; every other
    protocol stays ``parse_only``.
    """
    item_protocol: dict[str, str] = {}
    levels: dict[str, str] = {}
    for node in nodes or ():
        protocol = str(getattr(node, "protocol", "") or "").strip().lower()
        if not protocol:
            continue
        item_id = str(getattr(node, "item_id", "") or "")
        if item_id:
            item_protocol[item_id] = protocol
        levels.setdefault(protocol, ProtocolSupport.PARSE_ONLY.value)
    for result in probe_results or ():
        if not is_real_probe_ok(result):
            continue
        item_id = str(
            result.get("item_id")
            if isinstance(result, Mapping)
            else getattr(result, "item_id", "")
        )
        protocol = item_protocol.get(item_id)
        if protocol:
            levels[protocol] = ProtocolSupport.PROBE_SUPPORTED.value
    return levels


def support_report(
    nodes: Iterable[Any],
    probe_results: Iterable[Any] | None = None,
) -> dict[str, Any]:
    """Build the ``protocol_support`` block written into report.json."""
    return {
        "rule": SUPPORT_RULE,
        "levels": protocol_support_levels(nodes, probe_results),
        "level_values": list(SUPPORT_LEVELS),
    }


__all__ = [
    "SUPPORT_RULE",
    "SUPPORT_LEVELS",
    "parse_support_level",
    "is_real_probe_ok",
    "protocol_support_levels",
    "support_report",
]
