"""What every signature scheme needs: the request as it was signed, and the verdict."""

import hashlib
import hmac
from dataclasses import dataclass
from urllib.parse import quote, unquote

from starlette.datastructures import Headers
from starlette.requests import Request

from filen_s3_emulator.errors import S3Error

EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()

# Characters left alone when a path arrives with raw (unencoded) bytes; everything already
# percent-encoded passes through untouched.
_PATH_SAFE = "/%-_.~!$&'()*+,;=:@"


def parse_query(raw: str) -> list[tuple[str, str]]:
    """Split a query string into decoded pairs. ``+`` stays ``+`` (S3 is not form-encoded)."""
    pairs = []
    for item in raw.split("&"):
        if item:
            name, _, value = item.partition("=")
            pairs.append((unquote(name), unquote(value)))
    return pairs


@dataclass(frozen=True)
class RequestView:
    """The parts of a request a signature covers, exactly as they arrived on the wire."""

    method: str
    raw_path: str
    raw_query: str
    query: list[tuple[str, str]]
    headers: Headers

    @classmethod
    def from_request(cls, request: Request) -> "RequestView":
        raw_path = request.scope.get("raw_path") or request.scope["path"].encode()
        raw_query = request.scope.get("query_string", b"").decode("latin-1")
        return cls(
            method=request.method,
            raw_path=quote(raw_path, safe=_PATH_SAFE),
            raw_query=raw_query,
            query=parse_query(raw_query),
            headers=request.headers,
        )

    def query_value(self, name: str) -> str | None:
        for key, value in self.query:
            if key == name:
                return value
        return None


@dataclass(frozen=True)
class AuthResult:
    """A verified request. The SigV4 fields are set only when chunk signatures follow."""

    scheme: str
    payload_hash: str | None = None
    signing_key: bytes | None = None
    amz_date: str = ""
    scope: str = ""
    seed_signature: str = ""

    def chunk_signature(self, previous: str, chunk_sha256: str) -> str:
        string_to_sign = "\n".join(
            [
                "AWS4-HMAC-SHA256-PAYLOAD",
                self.amz_date,
                self.scope,
                previous,
                EMPTY_SHA256,
                chunk_sha256,
            ]
        )
        return hmac_hex(self.signing_key or b"", string_to_sign)

    def trailer_signature(self, previous: str, trailer: bytes) -> str:
        string_to_sign = "\n".join(
            [
                "AWS4-HMAC-SHA256-TRAILER",
                self.amz_date,
                self.scope,
                previous,
                hashlib.sha256(trailer).hexdigest(),
            ]
        )
        return hmac_hex(self.signing_key or b"", string_to_sign)


def hmac_hex(key: bytes, message: str) -> str:
    return hmac.new(key, message.encode(), hashlib.sha256).hexdigest()


def check_access_key(presented: str, expected: str) -> None:
    if not expected or not hmac.compare_digest(presented.encode(), expected.encode()):
        raise S3Error(
            "InvalidAccessKeyId",
            "The AWS Access Key Id you provided does not exist in our records.",
            403,
        )


def check_signature(presented: str, expected: str) -> None:
    if not hmac.compare_digest(presented.encode(), expected.encode()):
        raise S3Error(
            "SignatureDoesNotMatch",
            "The request signature we calculated does not match the signature you provided.",
            403,
        )
