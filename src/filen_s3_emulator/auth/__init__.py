"""Request authentication: every signature scheme a boto3 client can produce."""

from datetime import UTC, datetime

from fastapi import Request

from filen_s3_emulator.auth import sigv2, sigv4
from filen_s3_emulator.auth.common import AuthResult, RequestView, parse_query
from filen_s3_emulator.config import Settings
from filen_s3_emulator.errors import S3Error

__all__ = ["AuthResult", "RequestView", "authenticate", "parse_query"]


def authenticate(request: Request, settings: Settings) -> tuple[RequestView, AuthResult]:
    view = RequestView.from_request(request)
    key, secret = settings.s3_access_key_id, settings.s3_secret_access_key
    header = view.headers.get("authorization", "")
    names = {name for name, _ in view.query}

    if header.startswith(sigv4.ALGORITHM + " "):
        return view, sigv4.verify_header(view, key, secret, datetime.now(UTC))
    if header.startswith("AWS "):
        return view, sigv2.verify_header(view, key, secret)
    if header:
        raise S3Error("InvalidArgument", "Unsupported Authorization type.")
    if "X-Amz-Signature" in names:
        return view, sigv4.verify_query(view, key, secret, datetime.now(UTC))
    if "Signature" in names and "AWSAccessKeyId" in names:
        return view, sigv2.verify_query(view, key, secret)
    raise S3Error("AccessDenied", "Access Denied", 403)
