"""An in-memory stand-in for :class:`filen_s3_emulator.upstream.Upstream`.

It implements the same interface with S3 semantics, so the offline end-to-end tests check
this service's protocol handling. The gateway quirks themselves are covered against
botocore's Stubber in ``test_upstream.py``.
"""

import threading
import uuid
from datetime import UTC, datetime

from df_s3_filen_wrapper import Entry

from filen_s3_emulator.errors import S3Error
from filen_s3_emulator.upstream import ObjectInfo


class _Body:
    def __init__(self, data: bytes) -> None:
        self._data = data

    def iter_chunks(self, size: int):
        for start in range(0, len(self._data), size):
            yield self._data[start : start + size]

    def close(self) -> None:
        pass


class FakeUpstream:
    def __init__(self, buckets: tuple[str, ...] = ("test",)) -> None:
        self.buckets: dict[str, dict[str, tuple[bytes, datetime, str]]] = {b: {} for b in buckets}
        self.lock = threading.Lock()
        self.fail_puts = False

    def _objects(self, bucket: str) -> dict[str, tuple[bytes, datetime, str]]:
        if bucket not in self.buckets:
            raise S3Error("NoSuchBucket", "The specified bucket does not exist.", 404)
        return self.buckets[bucket]

    def list_buckets(self):
        return [(name, datetime(2026, 1, 1, tzinfo=UTC)) for name in sorted(self.buckets)]

    def create_bucket(self, bucket: str) -> None:
        self.buckets.setdefault(bucket, {})

    def head_bucket(self, bucket: str) -> None:
        self._objects(bucket)

    def delete_bucket(self, bucket: str) -> None:
        if self._objects(bucket):
            raise S3Error("BucketNotEmpty", "The bucket you tried to delete is not empty.", 409)
        del self.buckets[bucket]

    def stat(self, bucket: str, key: str) -> ObjectInfo:
        found = self._objects(bucket).get(key)
        if found is None:
            raise S3Error("NoSuchKey", "The specified key does not exist.", 404, Key=key)
        data, modified, etag = found
        return ObjectInfo(len(data), modified, f'"{etag}"', "application/octet-stream")

    def get(self, bucket: str, key: str, byte_range):
        data = self._objects(bucket)[key][0]
        if byte_range:
            start, end = byte_range
            # The real gateway refuses these; the service must never send one.
            assert 0 <= start <= end < len(data), f"out-of-bounds range {byte_range}"
            data = data[start : end + 1]
        return _Body(data), "application/octet-stream"

    def put(self, bucket: str, key: str, body, size: int, content_type):
        if self.fail_puts:
            raise S3Error("ServiceUnavailable", "The Filen gateway answered 500.", 503)
        data = body.read()
        assert len(data) == size, f"declared {size} bytes, body had {len(data)}"
        etag = str(uuid.uuid4())
        now = datetime.now(UTC)
        with self.lock:
            self._objects(bucket)[key] = (data, now, etag)
        return f'"{etag}"', now

    def copy(self, source_bucket: str, source_key: str, bucket: str, key: str):
        data = self._objects(source_bucket)[source_key][0]
        etag, now = str(uuid.uuid4()), datetime.now(UTC)
        self._objects(bucket)[key] = (data, now, etag)
        return etag, now

    def delete(self, bucket: str, key: str) -> None:
        self._objects(bucket).pop(key, None)

    def list(self, bucket: str, prefix: str, *, fresh: bool = False) -> list[Entry]:
        return sorted(
            (
                Entry(key, len(data), modified)
                for key, (data, modified, _) in self._objects(bucket).items()
                if key.startswith(prefix)
            ),
            key=lambda e: e.key,
        )
