"""Remote upload of gate-approved public files.

Only the explicit whitelist may leave the machine. Proxy credential
files stay local even when the publish gates released them into
``output/latest``.
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import urllib.error
import urllib.parse
import urllib.request
from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from nodebench.core.config import PublishUploadConfig
from nodebench.core.errors import ConfigError, PublishError
from nodebench.exporters.cf_addapi import CF_ADDAPI_NAME
from nodebench.exporters.cf_addcsv import CF_ADDCSV_NAME
from nodebench.exporters.manifest import MANIFEST_NAME
from nodebench.exporters.report import REPORT_NAME
from nodebench.sources.http_utils import (
    DEFAULT_USER_AGENT,
    HttpError,
    is_private_host,
    validate_url,
)

# Remote upload is narrower than the local publish directory: never send
# proxy credentials, raw subscriptions, or databases.
UPLOAD_WHITELIST = frozenset(
    {
        CF_ADDAPI_NAME,
        CF_ADDCSV_NAME,
        REPORT_NAME,
        MANIFEST_NAME,
    }
)

DEFAULT_TIMEOUT_S = 15.0
DEFAULT_MAX_BYTES = 2 * 1024 * 1024
GITHUB_API_ROOT = "https://api.github.com"
GITHUB_RAW_ROOT = "https://raw.githubusercontent.com"


def upload_error(code: str, message: str) -> PublishError:
    return PublishError(code=code, message=message)


def prepare_payloads(
    paths: Sequence[str | Path],
    *,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> list[tuple[str, bytes]]:
    """Validate and read every file before any network side effect."""
    prepared: list[tuple[str, bytes]] = []
    for raw in paths:
        path = Path(raw)
        name = path.name
        if name not in UPLOAD_WHITELIST:
            raise upload_error(
                "upload_not_allowed",
                f"file is not on the upload whitelist: {name}",
            )
        if not path.is_file():
            raise upload_error("upload_missing", f"upload file not found: {name}")
        data = path.read_bytes()
        if len(data) > int(max_bytes):
            raise upload_error(
                "upload_too_large",
                f"file exceeds upload size limit ({len(data)} > {max_bytes}): {name}",
            )
        prepared.append((name, data))
    return prepared


def select_uploadable(directory: str | Path) -> list[Path]:
    """List whitelist files present in a published directory."""
    root = Path(directory)
    selected: list[Path] = []
    for name in sorted(UPLOAD_WHITELIST):
        path = root / name
        if path.is_file():
            selected.append(path)
    return selected


class PublishUploader(ABC):
    """Upload gate-approved files and return their public URLs."""

    @abstractmethod
    def upload_files(self, paths: Sequence[str | Path], base_name: str) -> list[str]:
        """Upload *paths* and return one public URL per file."""


class FolderUploader(PublishUploader):
    """Copy files into a local directory the user syncs to Pages/R2."""

    def __init__(self, folder_path: str | Path) -> None:
        target = Path(folder_path)
        if not str(folder_path or "").strip():
            raise ConfigError(
                code="upload_folder_missing",
                message="publish.upload.folder_path is required for the folder backend",
            )
        self.folder_path = target

    def upload_files(self, paths: Sequence[str | Path], base_name: str) -> list[str]:
        prepared = prepare_payloads(paths)
        self.folder_path.mkdir(parents=True, exist_ok=True)
        urls: list[str] = []
        for name, data in prepared:
            destination = self.folder_path / name
            temporary = self.folder_path / f".{name}.upload-tmp"
            temporary.write_bytes(data)
            os.replace(temporary, destination)
            urls.append(str(destination.resolve()))
        return urls


class GithubUploader(PublishUploader):
    """PUT whitelisted files through the GitHub Contents API.

    Every payload is prepared and validated before the first request so a
    rejected file cannot leave a half-uploaded tree behind. The commit
    message always carries ``base_name`` (the run id).
    """

    def __init__(
        self,
        *,
        repository: str,
        branch: str = "public",
        path_prefix: str = "nodebench",
        token_env: str = "GITHUB_TOKEN",
        env: Mapping[str, str] | None = None,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        max_bytes: int = DEFAULT_MAX_BYTES,
        opener: Any = None,
    ) -> None:
        repo = str(repository or "").strip()
        if not repo or repo.count("/") != 1:
            raise ConfigError(
                code="upload_github_repository_invalid",
                message="publish.upload.github.repository must look like owner/repo",
            )
        self.repository = repo
        self.branch = str(branch or "public").strip() or "public"
        prefix_parts = [
            part
            for part in str(path_prefix or "").replace("\\", "/").split("/")
            if part and part not in {".", ".."}
        ]
        self.path_prefix = "/".join(prefix_parts)
        self.token_env = str(token_env or "GITHUB_TOKEN").strip() or "GITHUB_TOKEN"
        source = os.environ if env is None else env
        self.token = str(source.get(self.token_env, "") or "").strip()
        if not self.token:
            raise ConfigError(
                code="upload_token_missing",
                message=(
                    f"github upload token is missing; set the "
                    f"{self.token_env} environment variable"
                ),
            )
        self.timeout_s = float(timeout_s)
        self.max_bytes = int(max_bytes)
        self.opener = opener

    def _remote_path(self, name: str) -> str:
        if self.path_prefix:
            return f"{self.path_prefix}/{name}"
        return name

    def _put(self, remote_path: str, name: str, data: bytes, base_name: str) -> str:
        url = (
            f"{GITHUB_API_ROOT}/repos/{self.repository}/contents/"
            f"{urllib.parse.quote(remote_path)}"
        )
        body = json.dumps(
            {
                "message": f"nodebench publish {base_name}: {name}",
                "content": base64.b64encode(data).decode("ascii"),
                "branch": self.branch,
            }
        ).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=body,
            method="PUT",
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {self.token}",
                "User-Agent": DEFAULT_USER_AGENT,
                "Content-Type": "application/json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        open_fn = self.opener if self.opener is not None else urllib.request.urlopen
        try:
            with open_fn(request, timeout=self.timeout_s) as response:
                status = int(getattr(response, "status", 0) or response.getcode() or 0)
                response.read()
        except urllib.error.HTTPError as err:
            try:
                err.read()
            except Exception:
                pass
            if err.code in (401, 403):
                raise upload_error(
                    "upload_permission_denied",
                    f"github rejected the upload of {name}: permission denied",
                ) from err
            raise upload_error(
                "upload_failed",
                f"github upload failed for {name}: HTTP {err.code}",
            ) from err
        except (urllib.error.URLError, TimeoutError, OSError) as err:
            raise upload_error(
                "upload_failed",
                f"github upload failed for {name}: {type(err).__name__}",
            ) from err
        if status >= 400:
            if status in (401, 403):
                raise upload_error(
                    "upload_permission_denied",
                    f"github rejected the upload of {name}: permission denied",
                )
            raise upload_error(
                "upload_failed",
                f"github upload failed for {name}: HTTP {status}",
            )
        return f"{GITHUB_RAW_ROOT}/{self.repository}/{self.branch}/{remote_path}"

    def upload_files(self, paths: Sequence[str | Path], base_name: str) -> list[str]:
        # Prepare everything first: a whitelist or size failure must not
        # leave any remote file behind.
        prepared = prepare_payloads(paths, max_bytes=self.max_bytes)
        urls: list[str] = []
        for name, data in prepared:
            remote_path = self._remote_path(name)
            urls.append(self._put(remote_path, name, data, str(base_name)))
        return urls


class HttpUploader(PublishUploader):
    """PUT/POST each file to ``{url}/{name}`` with timeout and size caps."""

    def __init__(
        self,
        *,
        url: str,
        method: str = "PUT",
        timeout_s: float = DEFAULT_TIMEOUT_S,
        max_bytes: int = DEFAULT_MAX_BYTES,
        opener: Any = None,
    ) -> None:
        target = str(url or "").strip()
        if not target:
            raise ConfigError(
                code="upload_http_url_missing",
                message="publish.upload.http.url is required for the http backend",
            )
        try:
            validate_url(target)
        except HttpError as err:
            raise ConfigError(
                code="upload_blocked_host",
                message=f"publish.upload.http.url is not allowed: {err.message}",
            ) from err
        if is_private_host(urllib.parse.urlparse(target).hostname or ""):
            raise ConfigError(
                code="upload_blocked_host",
                message="publish.upload.http.url must not target a private host",
            )
        verb = str(method or "PUT").strip().upper()
        if verb not in {"PUT", "POST"}:
            raise ConfigError(
                code="upload_http_method_invalid",
                message="publish.upload.http.method must be PUT or POST",
            )
        self.url = target.rstrip("/")
        self.method = verb
        self.timeout_s = float(timeout_s)
        self.max_bytes = int(max_bytes)
        self.opener = opener

    def upload_files(self, paths: Sequence[str | Path], base_name: str) -> list[str]:
        prepared = prepare_payloads(paths, max_bytes=self.max_bytes)
        open_fn = self.opener if self.opener is not None else urllib.request.urlopen
        urls: list[str] = []
        for name, data in prepared:
            target = f"{self.url}/{urllib.parse.quote(name)}"
            try:
                validate_url(target)
            except HttpError as err:
                raise upload_error(
                    "upload_blocked_host",
                    f"http upload target is not allowed: {err.message}",
                ) from err
            request = urllib.request.Request(
                target,
                data=data,
                method=self.method,
                headers={
                    "Accept": "*/*",
                    "User-Agent": DEFAULT_USER_AGENT,
                    "Content-Type": "application/octet-stream",
                    "X-NodeBench-Run": str(base_name),
                },
            )
            try:
                with open_fn(request, timeout=self.timeout_s) as response:
                    status = int(
                        getattr(response, "status", 0) or response.getcode() or 0
                    )
                    response.read()
            except urllib.error.HTTPError as err:
                try:
                    err.read()
                except Exception:
                    pass
                raise upload_error(
                    "upload_failed",
                    f"http upload failed for {name}: HTTP {err.code}",
                ) from err
            except (urllib.error.URLError, TimeoutError, OSError) as err:
                raise upload_error(
                    "upload_failed",
                    f"http upload failed for {name}: {type(err).__name__}",
                ) from err
            if status >= 400:
                raise upload_error(
                    "upload_failed",
                    f"http upload failed for {name}: HTTP {status}",
                )
            urls.append(target)
        return urls


def create_uploader(
    config: PublishUploadConfig,
    *,
    env: Mapping[str, str] | None = None,
    opener: Any = None,
) -> PublishUploader:
    backend = str(config.backend or "").strip().lower()
    if backend == "folder":
        return FolderUploader(config.folder_path)
    if backend == "github":
        return GithubUploader(
            repository=config.github.repository,
            branch=config.github.branch,
            path_prefix=config.github.path_prefix,
            token_env=config.github.token_env,
            env=env,
            opener=opener,
        )
    if backend == "http":
        return HttpUploader(
            url=config.http.url,
            method=config.http.method,
            timeout_s=config.http.timeout_s,
            max_bytes=config.http.max_bytes,
            opener=opener,
        )
    raise ConfigError(
        code="upload_backend_unknown",
        message=(
            "publish.upload.backend must be one of: folder, github, http; "
            f"got {config.backend!r}"
        ),
    )


def upload_published_files(
    config: PublishUploadConfig,
    directory: str | Path,
    *,
    base_name: str,
    env: Mapping[str, str] | None = None,
    opener: Any = None,
) -> list[str]:
    """Upload whitelist files from a published directory when enabled."""
    if not bool(config.enabled):
        return []
    paths = select_uploadable(directory)
    if not paths:
        raise upload_error(
            "upload_nothing",
            f"no whitelisted files to upload under {directory}",
        )
    uploader = create_uploader(config, env=env, opener=opener)
    return uploader.upload_files(paths, base_name)


__all__ = [
    "DEFAULT_MAX_BYTES",
    "DEFAULT_TIMEOUT_S",
    "UPLOAD_WHITELIST",
    "FolderUploader",
    "GithubUploader",
    "HttpUploader",
    "PublishUploader",
    "create_uploader",
    "prepare_payloads",
    "select_uploadable",
    "upload_published_files",
    "upload_error",
]
