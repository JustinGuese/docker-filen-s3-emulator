"""AWS Signature Version 4: the Authorization header, presigned query strings, and the
seed signature that signed aws-chunked uploads chain from.

The canonical URI is the path exactly as it arrived (S3 does not normalise or re-encode
it), which is why :class:`RequestView` carries ``raw_path`` rather than the decoded path.
"""

import hashlib
import hmac
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from urllib.parse import quote

from filen_s3_emulator.auth.common import (
    AuthResult,
    RequestView,
    check_access_key,
    check_signature,
    hmac_hex,
)
from filen_s3_emulator.errors import S3Error

ALGORITHM = "AWS4-HMAC-SHA256"
AMZ_DATE_FORMAT = "%Y%m%dT%H%M%SZ"
MAX_SKEW = timedelta(minutes=15)
MAX_EXPIRES = 7 * 24 * 3600
UNSIGNED_PAYLOAD = "UNSIGNED-PAYLOAD"
SIGNED_STREAMING = {
    "STREAMING-AWS4-HMAC-SHA256-PAYLOAD",
    "STREAMING-AWS4-HMAC-SHA256-PAYLOAD-TRAILER",
}


def signing_key(secret: str, date: str, region: str, service: str) -> bytes:
    key = f"AWS4{secret}".encode()
    for part in (date, region, service, "aws4_request"):
        key = hmac.new(key, part.encode(), hashlib.sha256).digest()
    return key


def _encode(value: str) -> str:
    return quote(value, safe="-_.~")


def canonical_query(pairs: list[tuple[str, str]], exclude: frozenset[str] = frozenset()) -> str:
    encoded = sorted((_encode(k), _encode(v)) for k, v in pairs if k not in exclude)
    return "&".join(f"{k}={v}" for k, v in encoded)


def canonical_headers(view: RequestView, names: list[str], host: str | None) -> str:
    lines = []
    for name in names:
        values = [host] if name == "host" and host is not None else view.headers.getlist(name)
        lines.append(f"{name}:{','.join(' '.join(v.split()) for v in values)}\n")
    return "".join(lines)


def _parse_amz_date(value: str) -> datetime:
    try:
        return datetime.strptime(value, AMZ_DATE_FORMAT).replace(tzinfo=UTC)
    except ValueError as exc:
        raise S3Error("AccessDenied", f"Invalid date {value!r}.", 403) from exc


def _credential(value: str) -> tuple[str, str, str, str]:
    parts = value.split("/")
    if len(parts) != 5 or parts[4] != "aws4_request":
        raise S3Error("AuthorizationHeaderMalformed", f"Malformed credential {value!r}.")
    return parts[0], parts[1], parts[2], parts[3]


def _verify(
    view: RequestView,
    secret: str,
    *,
    credential: str,
    amz_date: str,
    signed_headers: str,
    payload_hash: str,
    presented: str,
    exclude: frozenset[str] = frozenset(),
) -> tuple[bytes, str]:
    _, date, region, service = _credential(credential)
    if not amz_date.startswith(date):
        raise S3Error("AuthorizationHeaderMalformed", "Credential date does not match X-Amz-Date.")
    scope = f"{date}/{region}/{service}/aws4_request"
    key = signing_key(secret, date, region, service)
    names = signed_headers.lower().split(";")

    # A proxy in front of this service may rewrite Host; the client signed the original,
    # which such a proxy usually passes on as X-Forwarded-Host.
    hosts: list[str | None] = [None]
    forwarded = view.headers.get("x-forwarded-host")
    if "host" in names and forwarded:
        hosts.append(forwarded.split(",")[0].strip())

    query = canonical_query(view.query, exclude)
    signature = ""
    for host in hosts:
        canonical_request = "\n".join(
            [
                view.method,
                view.raw_path,
                query,
                canonical_headers(view, names, host),
                ";".join(names),
                payload_hash,
            ]
        )
        string_to_sign = "\n".join(
            [ALGORITHM, amz_date, scope, hashlib.sha256(canonical_request.encode()).hexdigest()]
        )
        signature = hmac_hex(key, string_to_sign)
        if hmac.compare_digest(signature.encode(), presented.encode()):
            return key, scope
    check_signature(presented, signature)
    raise AssertionError("unreachable: check_signature raises on mismatch")


def verify_header(view: RequestView, access_key: str, secret: str, now: datetime) -> AuthResult:
    header = view.headers.get("authorization", "")
    fields = {}
    for part in header[len(ALGORITHM) :].split(","):
        name, _, value = part.strip().partition("=")
        fields[name] = value
    credential = fields.get("Credential")
    signed_headers = fields.get("SignedHeaders")
    presented = fields.get("Signature")
    if not credential or not signed_headers or not presented:
        raise S3Error("AuthorizationHeaderMalformed", "The authorization header is malformed.")
    check_access_key(_credential(credential)[0], access_key)

    amz_date = view.headers.get("x-amz-date")
    if not amz_date and view.headers.get("date"):
        try:
            parsed = parsedate_to_datetime(view.headers["date"])
        except (TypeError, ValueError) as exc:
            raise S3Error("AccessDenied", "Invalid Date header.", 403) from exc
        amz_date = parsed.astimezone(UTC).strftime(AMZ_DATE_FORMAT)
    if not amz_date:
        raise S3Error("AccessDenied", "Missing X-Amz-Date.", 403)
    if abs(now - _parse_amz_date(amz_date)) > MAX_SKEW:
        raise S3Error(
            "RequestTimeTooSkewed",
            "The difference between the request time and the current time is too large.",
            403,
        )

    payload_hash = view.headers.get("x-amz-content-sha256")
    if not payload_hash:
        raise S3Error("InvalidRequest", "Missing required header x-amz-content-sha256.")

    key, scope = _verify(
        view,
        secret,
        credential=credential,
        amz_date=amz_date,
        signed_headers=signed_headers,
        payload_hash=payload_hash,
        presented=presented,
    )
    if payload_hash in SIGNED_STREAMING:
        return AuthResult("v4-header", payload_hash, key, amz_date, scope, presented)
    return AuthResult("v4-header", payload_hash)


def verify_query(view: RequestView, access_key: str, secret: str, now: datetime) -> AuthResult:
    params = dict(view.query)
    if params.get("X-Amz-Algorithm") != ALGORITHM:
        raise S3Error("AuthorizationQueryParametersError", "X-Amz-Algorithm must be SigV4.")
    credential = params.get("X-Amz-Credential", "")
    amz_date = params.get("X-Amz-Date", "")
    signed_headers = params.get("X-Amz-SignedHeaders", "")
    presented = params.get("X-Amz-Signature", "")
    try:
        expires = int(params.get("X-Amz-Expires", ""))
    except ValueError:
        expires = -1
    if not (credential and amz_date and signed_headers and presented) or not (
        0 < expires <= MAX_EXPIRES
    ):
        raise S3Error(
            "AuthorizationQueryParametersError", "Query-string authentication is malformed."
        )
    check_access_key(_credential(credential)[0], access_key)

    signed_at = _parse_amz_date(amz_date)
    if now < signed_at - MAX_SKEW:
        raise S3Error("AccessDenied", "Request is not valid yet.", 403)
    if now > signed_at + timedelta(seconds=expires):
        raise S3Error("AccessDenied", "Request has expired.", 403)

    _verify(
        view,
        secret,
        credential=credential,
        amz_date=amz_date,
        signed_headers=signed_headers,
        payload_hash=params.get("X-Amz-Content-Sha256", UNSIGNED_PAYLOAD),
        presented=presented,
        exclude=frozenset({"X-Amz-Signature"}),
    )
    return AuthResult("v4-query", UNSIGNED_PAYLOAD)
