"""Core contracts and infrastructure: schema, config, errors, context, fields.

Domain packages depend on ``core``; ``core`` must not import them.
"""

from nodebench.core.errors import (
    ConfigError,
    NodeBenchError,
    ProbeError,
    PublishError,
    StorageError,
)
from nodebench.core.schema import SCHEMA_VERSION

__all__ = [
    "ConfigError",
    "NodeBenchError",
    "ProbeError",
    "PublishError",
    "SCHEMA_VERSION",
    "StorageError",
]
