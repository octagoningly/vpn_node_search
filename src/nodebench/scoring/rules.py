from __future__ import annotations

import math

SCORING_VERSION = "1"
SPEED_REF_MB_S = 10.0
STABILITY_INSUFFICIENT_PENALTY = 0.9

PROXY_CRITICAL_DIMS = ("latency_ms", "speed_mb_s")
ENDPOINT_CRITICAL_DIMS = ("latency_ms", "speed_mb_s", "host_compatible")

PROXY_DIMS = ("latency", "speed", "purity", "stability", "loss")
ENDPOINT_DIMS = ("compatibility", "latency", "speed", "loss")


def latency_score(latency_ms: float, max_latency_ms: float) -> float:
    if max_latency_ms <= 0:
        raise ValueError("max_latency_ms must be greater than zero")
    if latency_ms < 0:
        raise ValueError("latency must not be negative")
    return max(0.0, min(1.0, 1.0 - latency_ms / max_latency_ms))


def speed_score(speed_mb_s: float, ref_mb_s: float = SPEED_REF_MB_S) -> float:
    if speed_mb_s < 0:
        raise ValueError("speed must not be negative")
    if ref_mb_s <= 0:
        raise ValueError("speed reference must be positive")
    return max(0.0, min(1.0, 1.0 - math.exp(-speed_mb_s / ref_mb_s)))


def purity_score(risk: float) -> float:
    if not 0.0 <= risk <= 100.0:
        raise ValueError("risk must be between 0 and 100")
    return 1.0 - risk / 100.0


def loss_score(loss_pct: float) -> float:
    if not 0.0 <= loss_pct <= 100.0:
        raise ValueError("loss_pct must be between 0 and 100")
    return 1.0 - loss_pct / 100.0


def stability_score(availability_rate: float) -> float:
    if not 0.0 <= availability_rate <= 1.0:
        raise ValueError("availability_rate must be between 0 and 1")
    return float(availability_rate)


def weighted_score(
    weights: dict[str, float], dims: dict[str, float | None]
) -> tuple[float, dict[str, float]]:
    """Renormalized weighted mean over the dimensions that carry a value."""
    breakdown: dict[str, float] = {}
    total_weight = 0.0
    total = 0.0
    for name, value in dims.items():
        if value is None:
            continue
        weight = float(weights.get(name, 0.0))
        if weight <= 0:
            continue
        breakdown[name] = float(value)
        total_weight += weight
        total += weight * float(value)
    if total_weight <= 0:
        return 0.0, breakdown
    score = total / total_weight
    return max(0.0, min(1.0, score)), breakdown


__all__ = [
    "SCORING_VERSION",
    "SPEED_REF_MB_S",
    "STABILITY_INSUFFICIENT_PENALTY",
    "PROXY_CRITICAL_DIMS",
    "ENDPOINT_CRITICAL_DIMS",
    "PROXY_DIMS",
    "ENDPOINT_DIMS",
    "latency_score",
    "speed_score",
    "purity_score",
    "loss_score",
    "stability_score",
    "weighted_score",
]
