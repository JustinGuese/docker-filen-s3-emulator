"""GetObject / HeadObject / PutObject / CopyObject / DeleteObject."""

import re
from datetime import datetime
from email.utils import format_datetime, parsedate_to_datetime
from urllib.parse import unquote

from fastapi.responses import Response, StreamingResponse
from starlette.concurrency import iterate_in_threadpool

from filen_s3_emulator import s3xml
from filen_s3_emulator.body import spool
from filen_s3_emulator.errors import S3Error
from filen_s3_emulator.routes.common import S3Context, run, validate_key
from filen_s3_emulator.upstream import ObjectInfo

_RANGE = re.compile(r"bytes=(\d*)-(\d*)")
_OVERRIDES = {
    "response-content-type": "Content-Type",
    "response-content-disposition": "Content-Disposition",
    "response-content-encoding": "Content-Encoding",
    "response-content-language": "Content-Language",
    "response-cache-control": "Cache-Control",
    "response-expires": "Expires",
}
STREAM_CHUNK = 1024 * 1024


def parse_range(header: str | None, size: int) -> tuple[int, int] | None:
    """An in-bounds ``(start, end)``, or ``None`` to serve the whole object. S3 ignores a
    syntactically invalid or multi-part range; an unsatisfiable one is a 416."""
    match = _RANGE.fullmatch((header or "").strip())
    if not match or size == 0 or (not match[1] and not match[2]):
        return None
    if not match[1]:
        suffix = int(match[2])
        if suffix == 0:
            raise _invalid_range(size)
        return max(0, size - suffix), size - 1
    start = int(match[1])
    if start >= size:
        raise _invalid_range(size)
    end = int(match[2]) if match[2] else size - 1
    if end < start:
        return None
    return start, min(end, size - 1)


def _invalid_range(size: int) -> S3Error:
    return S3Error("InvalidRange", "The requested range is not satisfiable.", 416, size=str(size))


def _etags_match(header: str, etag: str) -> bool:
    wanted = etag.strip('"')
    return any(
        t.strip() == "*" or t.strip().removeprefix("W/").strip('"') == wanted
        for t in header.split(",")
    )


def _http_date(value: str) -> datetime | None:
    try:
        return parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None


def check_preconditions(headers, info: ObjectInfo) -> None:
    """RFC 7232 order. An ETag test is skipped when the ETag is unknown (HEAD-lag fallback)."""
    if_match, if_none_match = headers.get("if-match"), headers.get("if-none-match")
    modified = info.last_modified.replace(microsecond=0) if info.last_modified else None
    if if_match is not None and info.etag and not _etags_match(if_match, info.etag):
        raise S3Error("PreconditionFailed", "At least one of the preconditions failed.", 412)
    if (
        if_match is None
        and modified
        and (since := _http_date(headers.get("if-unmodified-since", "")))
    ):
        if modified > since:
            raise S3Error("PreconditionFailed", "At least one of the preconditions failed.", 412)
    if if_none_match is not None and info.etag and _etags_match(if_none_match, info.etag):
        raise S3Error("NotModified", "Not Modified", 304)
    if (
        if_none_match is None
        and modified
        and (since := _http_date(headers.get("if-modified-since", "")))
    ):
        if modified <= since:
            raise S3Error("NotModified", "Not Modified", 304)


def _object_headers(ctx: S3Context, info: ObjectInfo, content_type: str | None) -> dict[str, str]:
    headers = {"Accept-Ranges": "bytes", "Content-Type": content_type or "binary/octet-stream"}
    if etag := s3xml.quote_etag(info.etag):
        headers["ETag"] = etag
    if info.last_modified:
        headers["Last-Modified"] = format_datetime(info.last_modified, usegmt=True)
    for param, header in _OVERRIDES.items():
        if value := ctx.params.get(param):
            headers[header] = value
    return headers


async def get_object(ctx: S3Context, *, head: bool) -> Response:
    validate_key(ctx.key)
    info = await run(ctx.upstream.stat, ctx.bucket, ctx.key)
    check_preconditions(ctx.request.headers, info)
    byte_range = parse_range(ctx.request.headers.get("range"), info.size)
    status, length = 200, info.size
    extra = {}
    if byte_range:
        status, length = 206, byte_range[1] - byte_range[0] + 1
        extra["Content-Range"] = f"bytes {byte_range[0]}-{byte_range[1]}/{info.size}"

    if head:
        headers = _object_headers(ctx, info, info.content_type)
        return Response(
            status_code=status, headers={**headers, **extra, "Content-Length": str(length)}
        )

    whole = byte_range is None or byte_range == (0, info.size - 1)
    body, content_type = await run(
        ctx.upstream.get, ctx.bucket, ctx.key, None if whole else byte_range
    )
    headers = _object_headers(ctx, info, content_type or info.content_type)

    async def stream():
        try:
            async for chunk in iterate_in_threadpool(body.iter_chunks(STREAM_CHUNK)):
                yield chunk
        finally:
            body.close()

    return StreamingResponse(
        stream(), status_code=status, headers={**headers, **extra, "Content-Length": str(length)}
    )


async def put_object(ctx: S3Context) -> Response:
    validate_key(ctx.key)
    headers = ctx.request.headers
    if headers.get("if-none-match") is not None or headers.get("if-match") is not None:
        try:
            check_preconditions(headers, await run(ctx.upstream.stat, ctx.bucket, ctx.key))
        except S3Error as exc:
            if exc.code == "NoSuchKey" and headers.get("if-match") is None:
                pass
            elif exc.code == "NotModified":
                raise S3Error(
                    "PreconditionFailed", "At least one of the preconditions failed.", 412
                ) from exc
            else:
                raise

    spooled = await spool(ctx.request, ctx.auth, ctx.tmp_dir, ctx.settings.max_object_bytes)
    try:
        with spooled.path.open("rb") as handle:
            etag, _ = await run(
                ctx.upstream.put,
                ctx.bucket,
                ctx.key,
                handle,
                spooled.size,
                headers.get("content-type"),
            )
    finally:
        spooled.discard()
    return Response(headers={"ETag": s3xml.quote_etag(etag) or f'"{spooled.md5}"'})


async def copy_object(ctx: S3Context) -> Response:
    validate_key(ctx.key)
    source = unquote(ctx.request.headers["x-amz-copy-source"]).split("?", 1)[0].lstrip("/")
    source_bucket, _, source_key = source.partition("/")
    if not source_bucket or not source_key:
        raise S3Error("InvalidArgument", "Copy Source must mention the source bucket and key.")
    validate_key(source_key)

    info = await run(ctx.upstream.stat, source_bucket, source_key)
    if (source_bucket, source_key) == (ctx.bucket, ctx.key):
        # The gateway deletes the destination before copying, which here is the source.
        if ctx.request.headers.get("x-amz-metadata-directive", "").upper() != "REPLACE":
            raise S3Error(
                "InvalidRequest",
                "This copy request is illegal because it is trying to copy an object to itself "
                "without changing the object's metadata, storage class, website redirect "
                "location or encryption attributes.",
            )
        return Response(
            s3xml.copy_result(info.etag, info.last_modified), media_type="application/xml"
        )

    etag, last_modified = await run(
        ctx.upstream.copy, source_bucket, source_key, ctx.bucket, ctx.key
    )
    return Response(s3xml.copy_result(etag, last_modified), media_type="application/xml")


async def delete_object(ctx: S3Context) -> Response:
    validate_key(ctx.key)
    await run(ctx.upstream.delete, ctx.bucket, ctx.key)
    return Response(status_code=204)
