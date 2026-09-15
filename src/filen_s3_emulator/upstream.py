"""The only code that talks to the Filen gateway, and where its quirks are closed.

Built on ``df_s3_filen_wrapper`` (aws-chunked-safe client, marker-free listings), plus
what this service measured on top of that package's README:

* **Keys that need percent-encoding answer 401.** The gateway verifies signatures with
  ``@filen/aws4-express``, which URI-encodes each path segment of the *already encoded*
  path (``%20`` -> ``%2520``) -- the generic SigV4 rule, not S3's. Requests are signed
  here the same way, so spaces, unicode, ``+``, ``(``, ``:`` ... work. A literal ``%XX``
  in a key is still decoded twice by the gateway, so callers refuse such keys.
* **The ETag arrives as ``e-tag``**, which botocore does not parse; it is read raw.
* **``Range: bytes=-N`` is a 400** and a range ending past the object is a 400; callers
  resolve ranges to explicit, in-bounds ``a-b`` first.
* **HEAD answers 401 for about a second after a PUT**; :meth:`Upstream.stat` falls back
  to a listing then.
* **DeleteObject on ``dir/`` deletes the directory recursively**, contents included;
  :meth:`Upstream.delete` only lets that through for an empty directory.
"""

import threading
import time
from dataclasses import dataclass
from datetime import datetime
from email.utils import parsedate_to_datetime
from typing import IO
from urllib.parse import quote

from botocore import auth as botocore_auth
from botocore.config import Config
from botocore.exceptions import ClientError
from df_s3_filen_wrapper import Entry, FilenBucket, make_client

from filen_s3_emulator.errors import S3Error


class FilenSigV4Auth(botocore_auth.S3SigV4Auth):
    """S3 SigV4 with the canonical URI encoded once more, as the gateway expects."""

    def _normalize_url_path(self, path: str) -> str:
        return "/".join(quote(segment, safe="-_.~") for segment in path.split("/"))


botocore_auth.AUTH_TYPE_MAPS.setdefault("filen-s3v4", FilenSigV4Auth)


@dataclass(frozen=True)
class ObjectInfo:
    size: int
    last_modified: datetime | None
    etag: str | None
    content_type: str | None = None


def _headers(response: dict) -> dict[str, str]:
    return response.get("ResponseMetadata", {}).get("HTTPHeaders", {})


def _etag(headers: dict[str, str]) -> str | None:
    return headers.get("etag") or headers.get("e-tag")


def _http_date(value: str | None) -> datetime | None:
    try:
        return parsedate_to_datetime(value) if value else None
    except (TypeError, ValueError):
        return None


def _status(exc: ClientError) -> int:
    return int(exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode") or 500)


def translate(exc: ClientError, *, key: str | None = None) -> S3Error:
    status = _status(exc)
    code = str(exc.response.get("Error", {}).get("Code", ""))
    if status == 404:
        if key is None or code == "NoSuchBucket":
            return S3Error("NoSuchBucket", "The specified bucket does not exist.", 404)
        return S3Error("NoSuchKey", "The specified key does not exist.", 404, Key=key)
    if status == 429:
        return S3Error("SlowDown", "Please reduce your request rate.", 503)
    if status in (400, 409, 412) and code and not code.isdigit():
        return S3Error(code, exc.response["Error"].get("Message", code), status)
    return S3Error("ServiceUnavailable", f"The Filen gateway answered {status} {code}.", 503)


class Upstream:
    def __init__(
        self, endpoint: str, access_key: str, secret_key: str, *, region: str, cache_seconds: float
    ) -> None:
        self.client = make_client(
            endpoint,
            access_key,
            secret_key,
            region=region,
            config=Config(max_pool_connections=32, retries={"mode": "standard"}),
        )
        self.client.meta.events.register("choose-signer.s3", lambda **_: "filen-s3v4")
        self._cache_seconds = cache_seconds
        self._cache: dict[tuple[str, str], tuple[float, list[Entry]]] = {}
        self._lock = threading.Lock()

    def _bucket(self, bucket: str) -> FilenBucket:
        return FilenBucket(self.client, bucket)

    def _invalidate(self, bucket: str) -> None:
        with self._lock:
            for cached in [k for k in self._cache if k[0] == bucket]:
                del self._cache[cached]

    # ---- buckets ------------------------------------------------------------------------

    def list_buckets(self) -> list[tuple[str, datetime | None]]:
        try:
            buckets = self.client.list_buckets().get("Buckets", [])
        except ClientError as exc:
            raise translate(exc) from exc
        return [(b["Name"], b.get("CreationDate")) for b in buckets]

    def create_bucket(self, bucket: str) -> None:
        try:
            self._bucket(bucket).ensure()
        except ClientError as exc:
            raise translate(exc) from exc

    def head_bucket(self, bucket: str) -> None:
        try:
            self.client.head_bucket(Bucket=bucket)
        except ClientError as exc:
            raise translate(exc) from exc

    def delete_bucket(self, bucket: str) -> None:
        if self.list(bucket, "", fresh=True):
            raise S3Error("BucketNotEmpty", "The bucket you tried to delete is not empty.", 409)
        try:
            self.client.delete_bucket(Bucket=bucket)
        except ClientError as exc:
            raise translate(exc) from exc
        self._invalidate(bucket)

    # ---- objects ------------------------------------------------------------------------

    def stat(self, bucket: str, key: str) -> ObjectInfo:
        try:
            response = self.client.head_object(Bucket=bucket, Key=key)
        except ClientError as exc:
            if _status(exc) != 401:
                raise translate(exc, key=key) from exc
            try:
                entry = self._bucket(bucket).stat(key)
            except ClientError as inner:
                raise translate(inner, key=key) from inner
            if entry is None:
                raise translate(exc, key=key) from exc
            return ObjectInfo(entry.size, entry.last_modified, None)
        headers = _headers(response)
        return ObjectInfo(
            int(response.get("ContentLength", 0)),
            response.get("LastModified") or _http_date(headers.get("last-modified")),
            _etag(headers),
            headers.get("content-type"),
        )

    def get(self, bucket: str, key: str, byte_range: tuple[int, int] | None):
        """The streaming body and content type. ``byte_range`` must be in bounds."""
        kwargs = {"Bucket": bucket, "Key": key}
        if byte_range:
            kwargs["Range"] = f"bytes={byte_range[0]}-{byte_range[1]}"
        try:
            response = self.client.get_object(**kwargs)
        except ClientError as exc:
            raise translate(exc, key=key) from exc
        return response["Body"], _headers(response).get("content-type")

    def put(
        self, bucket: str, key: str, body: IO[bytes], size: int, content_type: str | None
    ) -> tuple[str | None, datetime | None]:
        extra = {"ContentType": content_type} if content_type else {}
        try:
            response = self.client.put_object(
                Bucket=bucket, Key=key, Body=body, ContentLength=size, **extra
            )
        except ClientError as exc:
            raise translate(exc, key=key) from exc
        finally:
            self._invalidate(bucket)
        headers = _headers(response)
        return _etag(headers), _http_date(headers.get("last-modified"))

    def copy(
        self, source_bucket: str, source_key: str, bucket: str, key: str
    ) -> tuple[str | None, datetime | None]:
        try:
            response = self.client.copy_object(
                Bucket=bucket, Key=key, CopySource={"Bucket": source_bucket, "Key": source_key}
            )
        except ClientError as exc:
            raise translate(exc, key=source_key) from exc
        finally:
            self._invalidate(bucket)
        result = response.get("CopyObjectResult", {})
        return result.get("ETag"), result.get("LastModified")

    def delete(self, bucket: str, key: str) -> None:
        """S3 semantics: a missing key is not an error, and ``dir/`` is never recursive."""
        # Never from the cache: a stale "empty" here would delete real objects.
        if key.endswith("/") and self.list(bucket, key, fresh=True):
            return
        try:
            self.client.delete_object(Bucket=bucket, Key=key)
        except ClientError as exc:
            if _status(exc) != 404:
                raise translate(exc, key=key) from exc
        finally:
            self._invalidate(bucket)

    def list(self, bucket: str, prefix: str, *, fresh: bool = False) -> list[Entry]:
        """Every object under ``prefix``, sorted by key, without directory markers."""
        now = time.monotonic()
        with self._lock:
            cached = self._cache.get((bucket, prefix))
        if cached and not fresh and now - cached[0] < self._cache_seconds:
            return cached[1]
        try:
            entries = sorted(self._bucket(bucket).iter(prefix), key=lambda e: e.key)
        except ClientError as exc:
            raise translate(exc) from exc
        with self._lock:
            self._cache[(bucket, prefix)] = (now, entries)
        return entries
