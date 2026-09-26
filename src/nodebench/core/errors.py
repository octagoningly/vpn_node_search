from __future__ import annotations

from nodebench.core.schema import ErrorInfo

EXIT_OK = 0
EXIT_CONFIG = 2
EXIT_SOURCES = 3
EXIT_PROBE_OR_STORAGE = 4
EXIT_EXPORT_OR_PUBLISH = 5


class NodeBenchError(Exception):
    """Base error carrying a redacted, structured :class:`ErrorInfo`."""

    def __init__(
        self,
        stage: str,
        code: str,
        message: str,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.stage = stage
        self.code = code
        self.message = message
        self.retryable = retryable
        self.info = ErrorInfo(
            stage=stage,
            code=code,
            message_redacted=message,
            retryable=retryable,
        )

    @property
    def message_redacted(self) -> str:
        return self.info.message_redacted

    def __str__(self) -> str:
        return f"[{self.stage}:{self.code}] {self.message}"


class ConfigError(NodeBenchError):
    def __init__(
        self,
        code: str = "config_error",
        message: str = "configuration error",
        retryable: bool = False,
    ) -> None:
        super().__init__(stage="config", code=code, message=message, retryable=retryable)


class SourceError(NodeBenchError):
    def __init__(
        self,
        code: str = "source_error",
        message: str = "source error",
        retryable: bool = True,
        stage: str = "collect",
    ) -> None:
        super().__init__(stage=stage, code=code, message=message, retryable=retryable)


class ParseError(NodeBenchError):
    def __init__(
        self,
        code: str = "parse_error",
        message: str = "parse error",
        retryable: bool = False,
    ) -> None:
        super().__init__(stage="parse", code=code, message=message, retryable=retryable)


class ProbeError(NodeBenchError):
    def __init__(
        self,
        code: str = "probe_error",
        message: str = "probe error",
        retryable: bool = False,
    ) -> None:
        super().__init__(stage="probe", code=code, message=message, retryable=retryable)


class StorageError(NodeBenchError):
    def __init__(
        self,
        code: str = "storage_error",
        message: str = "storage error",
        retryable: bool = False,
    ) -> None:
        super().__init__(stage="persist", code=code, message=message, retryable=retryable)


class ExportError(NodeBenchError):
    def __init__(
        self,
        code: str = "export_error",
        message: str = "export error",
        retryable: bool = False,
        stage: str = "export",
    ) -> None:
        super().__init__(stage=stage, code=code, message=message, retryable=retryable)


def exit_code_for(err: BaseException) -> int:
    """Map an error to the documented process exit code."""
    if isinstance(err, ConfigError):
        return EXIT_CONFIG
    if isinstance(err, SourceError):
        return EXIT_SOURCES
    if isinstance(err, (ProbeError, StorageError)):
        return EXIT_PROBE_OR_STORAGE
    if isinstance(err, ExportError):
        return EXIT_EXPORT_OR_PUBLISH
    if isinstance(err, ParseError):
        return EXIT_PROBE_OR_STORAGE
    if isinstance(err, NodeBenchError):
        return EXIT_PROBE_OR_STORAGE
    return EXIT_CONFIG


__all__ = [
    "EXIT_OK",
    "EXIT_CONFIG",
    "EXIT_SOURCES",
    "EXIT_PROBE_OR_STORAGE",
    "EXIT_EXPORT_OR_PUBLISH",
    "NodeBenchError",
    "ConfigError",
    "SourceError",
    "ParseError",
    "ProbeError",
    "StorageError",
    "ExportError",
    "exit_code_for",
]
