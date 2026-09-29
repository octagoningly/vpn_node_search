"""Application pipeline: collect → parse → normalize → probe → score → export → publish.

This package is the composition root. Domain packages stay free of each
other; only ``pipeline`` (and ``cli``) wire them together.
"""

from __future__ import annotations

from nodebench.pipeline.orchestrator import resolve_run_exit, run_pipeline
from nodebench.pipeline.stages import (
    export_artifacts,
    inspect_artifacts,
    publish_artifacts,
    run_post_stages,
    score_artifacts,
)

__all__ = [
    "export_artifacts",
    "inspect_artifacts",
    "publish_artifacts",
    "resolve_run_exit",
    "run_pipeline",
    "run_post_stages",
    "score_artifacts",
]
