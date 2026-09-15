"""Every signature scheme a plain boto3 client can produce, captured with botocore's own
``before-send`` hook and verified against this service's hand-rolled implementation."""

from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit

import boto3
import pytest
from botocore.awsrequest import AWSPreparedRequest
from botocore.config import Config
from starlette.datastructures import Headers

from filen_s3_emulator.auth import authenticate, sigv4
from filen_s3_emulator.auth.common import RequestView
from filen_s3_emulator.config import Settings
from filen_s3_emulator.errors import S3Error

ACCESS_KEY, SECRET_KEY = "AKIAEXAMPLE", "secretkeyvalue"
ENDPOINT = "https://filen-s3-emulator.example.test"
SETTINGS = Settings(s3_access_key_id=ACCESS_KEY, s3_secret_access_key=SECRET_KEY)


def _client(**config) -> boto3.client:
    return boto3.client(
        "s3",
        endpoint_url=ENDPOINT,
        aws_access_key_id=ACCESS_KEY,
        aws_secret_access_key=SECRET_KEY,
        region_name="us-east-1",
        config=Config(**config) if config else None,
    )


def _capture(op: str, kwargs: dict, **config) -> AWSPreparedRequest:
    """Run a boto3 call up to (but not including) the network, and return what it signed."""
    s3 = _client(**config)
    captured = {}

    def hook(request, **kw):
        captured["request"] = request
        raise SystemExit

    s3.meta.events.register("before-send", hook)
    try:
        getattr(s3, op)(**kwargs)
    except SystemExit:
        pass
    return captured["request"]


class _FakeASGIRequest:
    """Just enough of a Starlette ``Request`` for :func:`authenticate`: the scope it reads
    ``raw_path``/``query_string`` from, built from a request botocore actually prepared."""

    def __init__(self, method: str, url: str, headers: dict) -> None:
        parts = urlsplit(url)
        self.method = method
        # A real ASGI server always supplies Host, taken off the actual connection; botocore
        # signs against the same value (computed from the request URL when the header isn't
        # in its own dict yet) but only adds an explicit header once the wire client sends it.
        merged = {"host": parts.netloc, **{k: str(v) for k, v in headers.items()}}
        self.headers = Headers(headers=merged)
        self.scope = {
            "raw_path": parts.path.encode(),
            "query_string": parts.query.encode(),
            "path": parts.path,
        }

    @classmethod
    def from_prepared(cls, prepared: AWSPreparedRequest) -> "_FakeASGIRequest":
        # AWSPreparedRequest stores header values as bytes; decode them the way a real
        # ASGI server would before this service ever sees them.
        headers = {
            k: v.decode("latin-1") if isinstance(v, bytes) else v
            for k, v in prepared.headers.items()
        }
        return cls(prepared.method, prepared.url, headers)

    @classmethod
    def from_presigned_url(cls, url: str) -> "_FakeASGIRequest":
        return cls("GET", url, {})


def authenticate_as(request: _FakeASGIRequest, settings: Settings = SETTINGS):
    return authenticate(request, settings)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "op,kwargs",
    [
        ("put_object", {"Bucket": "b", "Key": "k.txt", "Body": b"data"}),
        ("get_object", {"Bucket": "b", "Key": "k.txt"}),
        ("list_objects_v2", {"Bucket": "b"}),
        ("delete_object", {"Bucket": "b", "Key": "dir/ü ñame (1)+x.bin"}),
    ],
)
def test_header_v4_verifies(op, kwargs):
    prepared = _capture(op, kwargs)
    _, result = authenticate_as(_FakeASGIRequest.from_prepared(prepared))
    assert result.scheme == "v4-header"


def test_query_v2_presign_verifies_by_default():
    url = _client().generate_presigned_url("get_object", Params={"Bucket": "b", "Key": "a b.txt"})
    assert "AWSAccessKeyId" in url
    _, result = authenticate_as(_FakeASGIRequest.from_presigned_url(url))
    assert result.scheme == "v2-query"


def test_query_v4_presign_verifies():
    url = _client(signature_version="s3v4").generate_presigned_url(
        "get_object", Params={"Bucket": "b", "Key": "a b.txt"}
    )
    assert "X-Amz-Signature" in url
    _, result = authenticate_as(_FakeASGIRequest.from_presigned_url(url))
    assert result.scheme == "v4-query"


def test_tampered_query_url_is_rejected():
    url = _client().generate_presigned_url("get_object", Params={"Bucket": "b", "Key": "a.txt"})
    tampered = url.replace("/a.txt", "/tampered.txt")
    with pytest.raises(S3Error) as exc:
        authenticate_as(_FakeASGIRequest.from_presigned_url(tampered))
    assert exc.value.code == "SignatureDoesNotMatch"


def test_wrong_secret_is_rejected():
    prepared = _capture("get_object", {"Bucket": "b", "Key": "a.txt"})
    wrong = Settings(s3_access_key_id=ACCESS_KEY, s3_secret_access_key="not-the-secret")
    with pytest.raises(S3Error) as exc:
        authenticate_as(_FakeASGIRequest.from_prepared(prepared), wrong)
    assert exc.value.code == "SignatureDoesNotMatch"


def test_unknown_access_key_is_rejected():
    prepared = _capture("get_object", {"Bucket": "b", "Key": "a.txt"})
    other = Settings(s3_access_key_id="someone-else", s3_secret_access_key=SECRET_KEY)
    with pytest.raises(S3Error) as exc:
        authenticate_as(_FakeASGIRequest.from_prepared(prepared), other)
    assert exc.value.code == "InvalidAccessKeyId"


def test_expired_query_presign_is_rejected():
    # A real sleep past a short ExpiresIn is flaky under wall-clock jitter (observed under
    # WSL2); inject `now` instead, the same way the skewed-clock header test does.
    url = _client(signature_version="s3v4").generate_presigned_url(
        "get_object", Params={"Bucket": "b", "Key": "a.txt"}, ExpiresIn=60
    )
    view = RequestView.from_request(_FakeASGIRequest.from_presigned_url(url))
    with pytest.raises(S3Error) as exc:
        sigv4.verify_query(view, ACCESS_KEY, SECRET_KEY, datetime.now(UTC) + timedelta(minutes=5))
    assert exc.value.code == "AccessDenied"
    assert exc.value.status == 403


def test_header_signature_with_skewed_clock_is_rejected():
    prepared = _capture("get_object", {"Bucket": "b", "Key": "a.txt"})
    request = _FakeASGIRequest.from_prepared(prepared)
    view = RequestView.from_request(request)
    with pytest.raises(S3Error) as exc:
        sigv4.verify_header(view, ACCESS_KEY, SECRET_KEY, datetime.now(UTC) + timedelta(hours=1))
    assert exc.value.code == "RequestTimeTooSkewed"


def test_header_v2_verifies():
    prepared = _capture("get_object", {"Bucket": "b", "Key": "a b.txt"}, signature_version="s3")
    _, result = authenticate_as(_FakeASGIRequest.from_prepared(prepared))
    assert result.scheme == "v2-header"
