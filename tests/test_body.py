"""aws-chunked decoding and the digest checks it enables: Content-MD5, x-amz-content-sha256,
chunk signatures, and x-amz-checksum-* as header or trailer."""

import asyncio
import hashlib

import pytest
from starlette.datastructures import Headers

from filen_s3_emulator import body
from filen_s3_emulator.auth.common import AuthResult
from filen_s3_emulator.errors import S3Error


class _FakeRequest:
    def __init__(self, headers: dict, chunks: list[bytes]) -> None:
        self.headers = Headers(headers=headers)
        self._chunks = chunks

    async def stream(self):
        for chunk in self._chunks:
            yield chunk


def unsigned_chunked(
    payload: bytes, checksum_header: str | None = None, checksum_value: str | None = None
) -> tuple[dict, list[bytes]]:
    """A minimal, unsigned aws-chunked body: one data chunk plus the terminator."""
    chunk = f"{len(payload):x}\r\n".encode() + payload + b"\r\n"
    trailer = f"{checksum_header}:{checksum_value}\r\n".encode() if checksum_header else b""
    body_bytes = chunk + b"0\r\n" + trailer + b"\r\n"
    headers = {
        "content-encoding": "aws-chunked",
        "x-amz-decoded-content-length": str(len(payload)),
    }
    if checksum_header:
        headers["x-amz-trailer"] = checksum_header
    return headers, [body_bytes]


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def auth() -> AuthResult:
    return AuthResult("v4-header", "STREAMING-UNSIGNED-PAYLOAD-TRAILER")


def test_decodes_aws_chunked_body(tmp_path, auth):
    payload = b"hello world" * 100
    headers, chunks = unsigned_chunked(payload)
    request = _FakeRequest(headers, chunks)
    spooled = run(body.spool(request, auth, tmp_path, max_bytes=10_000_000))
    assert spooled.path.read_bytes() == payload
    assert spooled.size == len(payload)
    spooled.discard()


def test_checksum_trailer_is_verified(tmp_path, auth):
    payload = b"checksum me"
    import base64
    import zlib

    crc = base64.b64encode(zlib.crc32(payload).to_bytes(4, "big")).decode()
    headers, chunks = unsigned_chunked(payload, "x-amz-checksum-crc32", crc)
    spooled = run(body.spool(_FakeRequest(headers, chunks), auth, tmp_path, max_bytes=1_000_000))
    spooled.discard()


def test_checksum_trailer_mismatch_is_rejected_and_spool_is_removed(tmp_path, auth):
    payload = b"checksum me"
    headers, chunks = unsigned_chunked(payload, "x-amz-checksum-crc32", "AAAAAA==")
    with pytest.raises(S3Error) as exc:
        run(body.spool(_FakeRequest(headers, chunks), auth, tmp_path, max_bytes=1_000_000))
    assert exc.value.code == "BadDigest"
    assert list(tmp_path.iterdir()) == []


def test_declared_length_mismatch_is_rejected(tmp_path, auth):
    payload = b"short"
    headers, chunks = unsigned_chunked(payload)
    headers["x-amz-decoded-content-length"] = "999"
    with pytest.raises(S3Error) as exc:
        run(body.spool(_FakeRequest(headers, chunks), auth, tmp_path, max_bytes=1_000_000))
    assert exc.value.code == "IncompleteBody"


def test_size_over_cap_is_rejected_mid_stream(tmp_path, auth):
    # A declared length under the cap, but the actual bytes exceed it -- caught while
    # writing, not just from the (attacker-controlled) header.
    payload = b"x" * 100
    headers, chunks = unsigned_chunked(payload)
    headers["x-amz-decoded-content-length"] = "1"
    with pytest.raises(S3Error) as exc:
        run(body.spool(_FakeRequest(headers, chunks), auth, tmp_path, max_bytes=50))
    assert exc.value.code in ("EntityTooLarge", "IncompleteBody")


def test_content_md5_is_verified(tmp_path):
    payload = b"plain body, no chunking"
    good = __import__("base64").b64encode(hashlib.md5(payload).digest()).decode()
    request = _FakeRequest({"content-length": str(len(payload)), "content-md5": good}, [payload])
    auth = AuthResult("v4-header", "UNSIGNED-PAYLOAD")
    spooled = run(body.spool(request, auth, tmp_path, max_bytes=1_000_000))
    spooled.discard()

    bad_request = _FakeRequest(
        {"content-length": str(len(payload)), "content-md5": "not-the-right-hash=="}, [payload]
    )
    with pytest.raises(S3Error) as exc:
        run(body.spool(bad_request, auth, tmp_path, max_bytes=1_000_000))
    assert exc.value.code == "BadDigest"


def test_hex_content_sha256_is_verified(tmp_path):
    payload = b"signed payload body"
    digest = hashlib.sha256(payload).hexdigest()
    auth = AuthResult("v4-header", digest)
    request = _FakeRequest({"content-length": str(len(payload))}, [payload])
    spooled = run(body.spool(request, auth, tmp_path, max_bytes=1_000_000))
    spooled.discard()

    wrong_auth = AuthResult("v4-header", "0" * 64)
    with pytest.raises(S3Error) as exc:
        run(
            body.spool(
                _FakeRequest({"content-length": str(len(payload))}, [payload]),
                wrong_auth,
                tmp_path,
                max_bytes=1_000_000,
            )
        )
    assert exc.value.code == "XAmzContentSHA256Mismatch"


def test_unsupported_checksum_algorithm_is_refused(tmp_path, auth):
    payload = b"x"
    headers = {
        "content-length": "1",
        "x-amz-checksum-crc64nvme": "AAAAAAAAAAA=",
    }
    with pytest.raises(S3Error) as exc:
        run(body.spool(_FakeRequest(headers, [payload]), auth, tmp_path, max_bytes=1000))
    assert exc.value.code == "InvalidRequest"
