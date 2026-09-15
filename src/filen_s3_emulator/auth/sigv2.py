"""AWS Signature Version 2 -- still what a default boto3 client presigns URLs with.

Mirrors botocore's ``HmacV1Auth`` string-to-sign, and reuses its list of sub-resources
so the two cannot drift apart.
"""

import base64
import hashlib
import hmac
import time
from email.utils import parsedate_to_datetime
from urllib.parse import unquote

from botocore.auth import HmacV1Auth

from filen_s3_emulator.auth.common import (
    AuthResult,
    RequestView,
    check_access_key,
    check_signature,
)
from filen_s3_emulator.errors import S3Error

SUBRESOURCES = frozenset(HmacV1Auth.QSAOfInterest)
MAX_SKEW_SECONDS = 15 * 60


def sign(secret: str, string_to_sign: str) -> str:
    digest = hmac.new(secret.encode(), string_to_sign.encode(), hashlib.sha1).digest()
    return base64.b64encode(digest).decode()


def canonical_resource(view: RequestView) -> str:
    pairs = []
    for item in view.raw_query.split("&"):
        name, sep, value = item.partition("=")
        if name in SUBRESOURCES:
            pairs.append(f"{name}={unquote(value)}" if sep else name)
    # botocore sorts by name only (stable), keeping the order of repeated names.
    pairs.sort(key=lambda pair: pair.partition("=")[0])
    return view.raw_path + ("?" + "&".join(pairs) if pairs else "")


def string_to_sign(
    view: RequestView, content_md5: str, content_type: str, date: str, amz: dict[str, str]
) -> str:
    lines = [view.method, content_md5, content_type, date]
    lines += [f"{name}:{amz[name]}" for name in sorted(amz)]
    return "\n".join(lines) + "\n" + canonical_resource(view)


def _amz_headers(view: RequestView) -> dict[str, str]:
    return {
        name: ",".join(v.strip() for v in view.headers.getlist(name))
        for name in {k.lower() for k in view.headers.keys()}
        if name.startswith("x-amz-")
    }


def verify_header(view: RequestView, access_key: str, secret: str) -> AuthResult:
    presented_key, _, presented = view.headers.get("authorization", "")[4:].rpartition(":")
    check_access_key(presented_key, access_key)
    date = view.headers.get("date", "")
    stamp = view.headers.get("x-amz-date") or date
    try:
        signed_at = parsedate_to_datetime(stamp).timestamp()
    except (TypeError, ValueError) as exc:
        raise S3Error("AccessDenied", "Missing or invalid Date header.", 403) from exc
    if abs(time.time() - signed_at) > MAX_SKEW_SECONDS:
        raise S3Error(
            "RequestTimeTooSkewed",
            "The difference between the request time and the current time is too large.",
            403,
        )
    expected = sign(
        secret,
        string_to_sign(
            view,
            view.headers.get("content-md5", ""),
            view.headers.get("content-type", ""),
            date,
            _amz_headers(view),
        ),
    )
    check_signature(presented, expected)
    return AuthResult("v2-header")


def verify_query(view: RequestView, access_key: str, secret: str) -> AuthResult:
    params = dict(view.query)
    check_access_key(params.get("AWSAccessKeyId", ""), access_key)
    expires = params.get("Expires", "")
    if not expires.isdigit():
        raise S3Error("AccessDenied", "Query-string authentication requires Expires.", 403)
    if time.time() > int(expires):
        raise S3Error("AccessDenied", "Request has expired.", 403)

    # botocore moves x-amz-*, Content-Type and Content-MD5 into the query string when it
    # presigns; a client may instead send them as headers.
    amz = _amz_headers(view)
    amz.update({k.lower(): v for k, v in view.query if k.lower().startswith("x-amz-")})
    content_md5 = params.get("content-md5", view.headers.get("content-md5", ""))
    candidates = {params.get("content-type", view.headers.get("content-type", "")), ""}

    presented = params.get("Signature", "")
    expected = ""
    for content_type in candidates:
        expected = sign(secret, string_to_sign(view, content_md5, content_type, expires, amz))
        if hmac.compare_digest(expected.encode(), presented.encode()):
            return AuthResult("v2-query")
    check_signature(presented, expected)
    raise AssertionError("unreachable: check_signature raises on mismatch")
