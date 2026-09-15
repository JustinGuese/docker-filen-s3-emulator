"""Request bodies: decode, verify, and spool to disk before anything goes upstream.

A current boto3 sends every upload as ``Content-Encoding: aws-chunked`` with a checksum
trailer. That framing is decoded here -- the gateway would store it verbatim -- and every
digest the client declared (Content-MD5, a hex ``x-amz-content-sha256``, chunk signatures,
``x-amz-checksum-*`` as header or trailer) is checked. A mismatch deletes the spool file,
so a corrupted body never reaches Filen.
"""

import base64
import hashlib
import hmac
import os
import tempfile
import zlib
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import crc32c
from fastapi import Request

from filen_s3_emulator.auth import AuthResult
from filen_s3_emulator.errors import S3Error

MAX_LINE = 8192
MAX_CHUNK = 64 * 1024**2
STREAMING = {
    "STREAMING-UNSIGNED-PAYLOAD-TRAILER",
    "STREAMING-AWS4-HMAC-SHA256-PAYLOAD",
    "STREAMING-AWS4-HMAC-SHA256-PAYLOAD-TRAILER",
}


class _Crc:
    def __init__(self, fn) -> None:
        self._fn, self._value = fn, 0

    def update(self, data: bytes) -> None:
        self._value = self._fn(data, self._value)

    def digest(self) -> bytes:
        return self._value.to_bytes(4, "big")


CHECKSUMS: dict[str, Callable[[], Any]] = {
    "crc32": lambda: _Crc(zlib.crc32),
    "crc32c": lambda: _Crc(lambda data, value: crc32c.crc32c(data, value=value)),
    "sha1": hashlib.sha1,
    "sha256": hashlib.sha256,
}
# Everything S3 accepts; the ones missing from CHECKSUMS are refused rather than skipped.
KNOWN_CHECKSUMS = (*CHECKSUMS, "crc64nvme")


@dataclass
class Spooled:
    path: Path
    size: int
    md5: str

    def discard(self) -> None:
        self.path.unlink(missing_ok=True)


class _Reader:
    """Line and exact-length reads over the ASGI byte stream."""

    def __init__(self, stream: AsyncIterator[bytes]) -> None:
        self._stream, self._buffer = stream, bytearray()

    async def _fill(self) -> None:
        async for chunk in self._stream:
            if chunk:
                self._buffer += chunk
                return
        raise S3Error("IncompleteBody", "The request body ended early.")

    async def line(self) -> bytes:
        while (end := self._buffer.find(b"\r\n")) < 0:
            if len(self._buffer) > MAX_LINE:
                raise S3Error("InvalidRequest", "Malformed aws-chunked encoding.")
            await self._fill()
        line = bytes(self._buffer[:end])
        del self._buffer[: end + 2]
        return line

    async def exactly(self, size: int) -> AsyncIterator[bytes]:
        while size:
            if not self._buffer:
                await self._fill()
            piece = bytes(self._buffer[:size])
            del self._buffer[: len(piece)]
            size -= len(piece)
            yield piece


def _checksum_spec(request: Request) -> tuple[str | None, str | None, bool]:
    """``(algorithm, expected_b64, expected_in_trailer)`` declared by the client."""
    headers = request.headers
    trailer = headers.get("x-amz-trailer", "").strip().lower().removeprefix("x-amz-checksum-")
    if trailer in KNOWN_CHECKSUMS:
        return trailer, None, True
    for algorithm in KNOWN_CHECKSUMS:
        if value := headers.get(f"x-amz-checksum-{algorithm}"):
            return algorithm, value, False
    return None, None, False


async def spool(request: Request, auth: AuthResult, directory: Path, max_bytes: int) -> Spooled:
    directory.mkdir(parents=True, exist_ok=True)
    payload = auth.payload_hash or ""
    chunked = payload in STREAMING or "aws-chunked" in request.headers.get("content-encoding", "")
    declared = request.headers.get("x-amz-decoded-content-length" if chunked else "content-length")
    if declared and declared.isdigit() and int(declared) > max_bytes:
        raise _too_large(max_bytes)

    algorithm, expected, in_trailer = _checksum_spec(request)
    if algorithm and algorithm not in CHECKSUMS:
        raise S3Error("InvalidRequest", f"Checksum algorithm {algorithm.upper()} is not supported.")
    checksum = CHECKSUMS[algorithm]() if algorithm else None
    md5 = hashlib.md5()
    sha256 = hashlib.sha256() if len(payload) == 64 else None

    fd, name = tempfile.mkstemp(dir=directory, prefix=".spool-")
    path, size = Path(name), 0
    try:
        with os.fdopen(fd, "wb") as out:

            def write(data: bytes) -> None:
                nonlocal size
                size += len(data)
                if size > max_bytes:
                    raise _too_large(max_bytes)
                out.write(data)
                md5.update(data)
                if checksum:
                    checksum.update(data)
                if sha256:
                    sha256.update(data)

            if chunked:
                trailers = await _decode_chunked(_Reader(request.stream()), auth, write)
                if in_trailer:
                    expected = trailers.get(f"x-amz-checksum-{algorithm}")
            else:
                async for data in request.stream():
                    write(data)

        if declared and declared.isdigit() and int(declared) != size:
            raise S3Error("IncompleteBody", "The body does not match the declared length.")
        if content_md5 := request.headers.get("content-md5"):
            if not _same(content_md5, base64.b64encode(md5.digest()).decode()):
                raise S3Error("BadDigest", "The Content-MD5 you specified did not match.")
        if sha256 and sha256.hexdigest() != payload:
            raise S3Error(
                "XAmzContentSHA256Mismatch",
                "The provided 'x-amz-content-sha256' header does not match what was computed.",
            )
        if checksum and expected is not None:
            actual = base64.b64encode(checksum.digest()).decode()
            if not _same(actual, expected.strip()):
                raise S3Error(
                    "BadDigest", f"The {str(algorithm).upper()} you specified did not match."
                )
        return Spooled(path, size, md5.hexdigest())
    except BaseException:
        path.unlink(missing_ok=True)
        raise


async def _decode_chunked(reader: _Reader, auth: AuthResult, write) -> dict[str, str]:
    signed = auth.signing_key is not None
    previous = auth.seed_signature
    while True:
        size_hex, _, extension = (await reader.line()).partition(b";")
        try:
            size = int(size_hex, 16)
        except ValueError as exc:
            raise S3Error("InvalidRequest", "Malformed aws-chunked encoding.") from exc
        if size > MAX_CHUNK:
            raise S3Error("InvalidRequest", "aws-chunked chunk too large.")
        digest = hashlib.sha256() if signed else None
        async for piece in reader.exactly(size):
            write(piece)
            if digest:
                digest.update(piece)
        if digest:
            presented = extension.decode().partition("chunk-signature=")[2]
            expected = auth.chunk_signature(previous, digest.hexdigest())
            if not _same(presented, expected):
                raise S3Error("SignatureDoesNotMatch", "A chunk signature does not match.", 403)
            previous = presented
        if size == 0:
            break
        if await reader.line():
            raise S3Error("InvalidRequest", "Malformed aws-chunked encoding.")

    trailers: dict[str, str] = {}
    trailer_bytes = b""
    while line := await reader.line():
        name, _, value = line.decode().partition(":")
        if name.strip().lower() == "x-amz-trailer-signature":
            if signed:
                expected = auth.trailer_signature(previous, trailer_bytes)
                if not _same(value.strip(), expected):
                    raise S3Error(
                        "SignatureDoesNotMatch", "The trailer signature does not match.", 403
                    )
            continue
        trailers[name.strip().lower()] = value.strip()
        trailer_bytes += line + b"\n"
    return trailers


def _same(presented: str, expected: str) -> bool:
    return hmac.compare_digest(presented.encode(), expected.encode())


def _too_large(max_bytes: int) -> S3Error:
    return S3Error(
        "EntityTooLarge",
        "Your proposed upload exceeds the maximum allowed size.",
        MaxSizeAllowed=str(max_bytes),
    )


async def read_small(request: Request, auth: AuthResult, directory: Path) -> bytes:
    """A small XML request body (DeleteObjects, CompleteMultipartUpload), fully verified."""
    spooled = await spool(request, auth, directory, max_bytes=4 * 1024**2)
    try:
        return spooled.path.read_bytes()
    finally:
        spooled.discard()
