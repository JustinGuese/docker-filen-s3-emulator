"""Multipart uploads, staged on local disk. The gateway has no multipart API.

Layout, one directory per upload -- no database::

    <staging>/uploads/<upload_id>/upload.json        {"bucket", "key", "initiated"}
    <staging>/uploads/<upload_id>/00001.<md5>.part   one file per part number

The part's MD5 (its ETag) is in its file name, so committing a part is a single atomic
rename. Completing concatenates the parts into one upstream PUT via :class:`ConcatReader`.
"""

import bisect
import io
import json
import re
import secrets
import shutil
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from filen_s3_emulator.body import Spooled
from filen_s3_emulator.errors import S3Error

MIN_PART_BYTES = 5 * 1024**2
MAX_PART_NUMBER = 10000
_PART = re.compile(r"^(\d{5})\.([0-9a-f]{32})\.part$")
_UPLOAD_ID = re.compile(r"^[A-Za-z0-9_-]{16,128}$")


@dataclass(frozen=True)
class Part:
    number: int
    etag: str
    size: int
    last_modified: datetime
    path: Path


@dataclass(frozen=True)
class Upload:
    upload_id: str
    bucket: str
    key: str
    initiated: datetime


def _no_such_upload() -> S3Error:
    return S3Error(
        "NoSuchUpload",
        "The specified multipart upload does not exist. The upload ID may be invalid, or the "
        "upload may have been aborted or completed.",
        404,
    )


class MultipartStore:
    def __init__(self, staging_dir: Path) -> None:
        self.root = staging_dir / "uploads"

    def _dir(self, upload_id: str) -> Path:
        if not _UPLOAD_ID.match(upload_id):
            raise _no_such_upload()
        return self.root / upload_id

    def create(self, bucket: str, key: str) -> str:
        upload_id = secrets.token_urlsafe(32)
        directory = self._dir(upload_id)
        directory.mkdir(parents=True)
        initiated = datetime.now(UTC).isoformat()
        (directory / "upload.json").write_text(
            json.dumps({"bucket": bucket, "key": key, "initiated": initiated})
        )
        return upload_id

    def get(self, upload_id: str, bucket: str, key: str) -> Upload:
        try:
            meta = json.loads((self._dir(upload_id) / "upload.json").read_text())
        except (FileNotFoundError, NotADirectoryError, json.JSONDecodeError) as exc:
            raise _no_such_upload() from exc
        if meta["bucket"] != bucket or meta["key"] != key:
            raise _no_such_upload()
        return Upload(upload_id, bucket, key, datetime.fromisoformat(meta["initiated"]))

    def staging(self, upload_id: str) -> Path:
        """Where a part being received is spooled: the upload's own directory, so the
        commit is a same-filesystem rename."""
        return self._dir(upload_id)

    def commit_part(self, upload: Upload, number: int, spooled: Spooled) -> str:
        if not 1 <= number <= MAX_PART_NUMBER:
            spooled.discard()
            raise S3Error("InvalidArgument", "Part number must be between 1 and 10000.")
        directory = self._dir(upload.upload_id)
        target = directory / f"{number:05d}.{spooled.md5}.part"
        spooled.path.replace(target)
        for stale in directory.glob(f"{number:05d}.*.part"):
            if stale != target:
                stale.unlink(missing_ok=True)
        return spooled.md5

    def parts(self, upload: Upload) -> list[Part]:
        found: dict[int, Part] = {}
        for path in self._dir(upload.upload_id).iterdir():
            match = _PART.match(path.name)
            if not match:
                continue
            try:
                stat = path.stat()
            except FileNotFoundError:
                continue
            part = Part(
                int(match[1]),
                match[2],
                stat.st_size,
                datetime.fromtimestamp(stat.st_mtime, UTC),
                path,
            )
            # Two files for one number only during a concurrent re-upload; newest wins.
            if part.number not in found or part.last_modified > found[part.number].last_modified:
                found[part.number] = part
        return [found[n] for n in sorted(found)]

    def uploads(self, bucket: str) -> list[Upload]:
        result: list[Upload] = []
        if not self.root.exists():
            return result
        for directory in self.root.iterdir():
            try:
                meta = json.loads((directory / "upload.json").read_text())
            except (FileNotFoundError, NotADirectoryError, json.JSONDecodeError):
                continue
            if meta["bucket"] == bucket:
                result.append(
                    Upload(
                        directory.name,
                        bucket,
                        meta["key"],
                        datetime.fromisoformat(meta["initiated"]),
                    )
                )
        return sorted(result, key=lambda u: (u.key, u.initiated))

    def assemble(
        self, upload: Upload, requested: list[tuple[int, str]], max_bytes: int
    ) -> "ConcatReader":
        """Validate the client's part list and return one reader over the chosen parts."""
        available = {part.number: part for part in self.parts(upload)}
        chosen: list[Part] = []
        for index, (number, etag) in enumerate(requested):
            if index and number <= requested[index - 1][0]:
                raise S3Error("InvalidPartOrder", "The list of parts was not in ascending order.")
            part = available.get(number)
            if part is None or part.etag != etag.lower():
                raise S3Error("InvalidPart", f"Part {number} was not found or its ETag differs.")
            chosen.append(part)
        for part in chosen[:-1]:
            if part.size < MIN_PART_BYTES:
                raise S3Error(
                    "EntityTooSmall",
                    "Your proposed upload is smaller than the minimum allowed object size.",
                )
        reader = ConcatReader([(p.path, p.size) for p in chosen])
        if reader.size > max_bytes:
            raise S3Error(
                "EntityTooLarge",
                "Your proposed upload exceeds the maximum allowed size.",
                MaxSizeAllowed=str(max_bytes),
            )
        return reader

    def abort(self, upload_id: str) -> None:
        shutil.rmtree(self._dir(upload_id), ignore_errors=True)

    def sweep(self, max_age_seconds: float) -> int:
        """Remove uploads untouched for ``max_age_seconds``, and spool files left behind by
        a request that died mid-body. Returns how many uploads were removed."""
        cutoff, removed = time.time() - max_age_seconds, 0
        spools = self.root.parent / "tmp"
        for path in spools.glob(".spool-*") if spools.exists() else []:
            _unlink_if_older(path, cutoff)
        for directory in self.root.iterdir() if self.root.exists() else []:
            try:
                newest = max(
                    (p.stat().st_mtime for p in directory.iterdir()),
                    default=directory.stat().st_mtime,
                )
            except FileNotFoundError:
                continue
            if newest < cutoff:
                shutil.rmtree(directory, ignore_errors=True)
                removed += 1
        return removed


def _unlink_if_older(path: Path, cutoff: float) -> None:
    try:
        if path.stat().st_mtime < cutoff:
            path.unlink()
    except FileNotFoundError:
        pass


class ConcatReader(io.RawIOBase):
    """One seekable, read-only stream over several files, so botocore can hash it for the
    signature and rewind it for a retry without holding the object in memory."""

    def __init__(self, files: list[tuple[Path, int]]) -> None:
        super().__init__()
        self._paths = [path for path, _ in files]
        self._starts: list[int] = []
        total = 0
        for _, size in files:
            self._starts.append(total)
            total += size
        self.size = total
        self._position = 0
        self._handle: tuple[int, io.BufferedReader] | None = None

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def tell(self) -> int:
        return self._position

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        base = {io.SEEK_SET: 0, io.SEEK_CUR: self._position, io.SEEK_END: self.size}[whence]
        self._position = max(0, base + offset)
        return self._position

    def readinto(self, buffer) -> int:
        if self._position >= self.size or not self._paths:
            return 0
        index = bisect.bisect_right(self._starts, self._position) - 1
        end = self._starts[index + 1] if index + 1 < len(self._starts) else self.size
        if self._handle is None or self._handle[0] != index:
            self._close_handle()
            self._handle = (index, self._paths[index].open("rb"))
        handle = self._handle[1]
        handle.seek(self._position - self._starts[index])
        view = memoryview(buffer)[: min(len(buffer), end - self._position)]
        count = handle.readinto(view)
        if not count:
            raise S3Error("InternalError", "A staged part was truncated.", 500)
        self._position += count
        return count

    def _close_handle(self) -> None:
        if self._handle is not None:
            self._handle[1].close()
            self._handle = None

    def close(self) -> None:
        self._close_handle()
        super().close()
