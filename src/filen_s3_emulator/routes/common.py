"""Per-request context shared by the S3 routes."""

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import unquote

from fastapi import Request
from starlette.concurrency import run_in_threadpool

from filen_s3_emulator.auth import AuthResult, RequestView, authenticate
from filen_s3_emulator.config import Settings
from filen_s3_emulator.errors import S3Error
from filen_s3_emulator.multipart import MultipartStore
from filen_s3_emulator.upstream import Upstream

_PERCENT_ESCAPE = re.compile(r"%[0-9A-Fa-f]{2}")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


async def run(fn, *args: Any, **kwargs: Any) -> Any:
    return await run_in_threadpool(fn, *args, **kwargs)


@dataclass
class S3Context:
    request: Request
    view: RequestView
    auth: AuthResult
    settings: Settings
    upstream: Upstream
    store: MultipartStore
    bucket: str
    key: str

    @property
    def params(self) -> dict[str, str]:
        return dict(self.view.query)

    @property
    def tmp_dir(self) -> Path:
        return Path(self.settings.staging_dir) / "tmp"


def split_path(request: Request) -> tuple[str, str]:
    raw = request.scope.get("raw_path") or request.scope["path"].encode()
    try:
        path = unquote(raw.decode("latin-1"), encoding="utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise S3Error("InvalidURI", "Couldn't parse the specified URI.") from exc
    bucket, _, key = path.lstrip("/").partition("/")
    return bucket, key


async def context(request: Request) -> S3Context:
    state = request.app.state
    view, auth = authenticate(request, state.settings)
    bucket, key = split_path(request)
    return S3Context(request, view, auth, state.settings, state.upstream, state.store, bucket, key)


def validate_key(key: str) -> None:
    """Refuse keys the gateway would silently store under a different name.

    It trims the key, collapses ``//``, resolves ``.`` and ``..`` segments, and
    percent-decodes the path once more than it should (so ``a%20b`` becomes ``a b``).
    """
    if not key:
        raise S3Error("InvalidArgument", "An object key must not be empty.")
    if len(key.encode()) > 1024:
        raise S3Error("KeyTooLongError", "Your key is too long.")
    segments = key.split("/")
    if (
        key != key.strip()
        or key.startswith("/")
        or "//" in key
        or any(segment in (".", "..") for segment in segments)
        or _PERCENT_ESCAPE.search(key)
        or _CONTROL.search(key)
    ):
        raise S3Error(
            "InvalidArgument",
            "This key cannot be stored on Filen unchanged: no leading or trailing whitespace, "
            "'//', '.' or '..' segments, control characters or '%XX' sequences.",
            ArgumentName="key",
            ArgumentValue=key,
        )


def validate_prefix(prefix: str) -> None:
    if _PERCENT_ESCAPE.search(prefix) or _CONTROL.search(prefix):
        raise S3Error("InvalidArgument", "This prefix cannot be listed on Filen.")


def refuse_subresources(params: dict[str, str], unsupported: frozenset[str]) -> None:
    for name in params:
        if name in unsupported:
            raise S3Error(
                "NotImplemented",
                f"The '{name}' sub-resource is not supported by this service.",
                501,
            )
