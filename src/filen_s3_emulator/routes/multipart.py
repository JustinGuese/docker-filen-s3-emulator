"""CreateMultipartUpload / UploadPart / CompleteMultipartUpload / AbortMultipartUpload /
ListParts / ListMultipartUploads."""

import asyncio
import logging

from fastapi.responses import Response, StreamingResponse

from filen_s3_emulator import s3xml
from filen_s3_emulator.body import read_small, spool
from filen_s3_emulator.errors import XML_DECLARATION, S3Error, error_body
from filen_s3_emulator.routes.common import S3Context, run, validate_key

log = logging.getLogger(__name__)

# Cloudflare drops a request whose response has not started within 100 s, and the single
# upstream PUT of a large upload takes longer than that. Like S3 itself, answer 200 at once
# and keep the connection alive with whitespace until the result document is ready.
KEEPALIVE_SECONDS = 10.0


def _xml(body: bytes, status: int = 200) -> Response:
    return Response(body, status_code=status, media_type="application/xml")


async def create(ctx: S3Context) -> Response:
    validate_key(ctx.key)
    upload_id = await run(ctx.store.create, ctx.bucket, ctx.key)
    return _xml(s3xml.initiate_multipart(ctx.bucket, ctx.key, upload_id))


async def upload_part(ctx: S3Context) -> Response:
    if "x-amz-copy-source" in ctx.request.headers:
        raise S3Error("NotImplemented", "UploadPartCopy is not supported.", 501)
    upload = ctx.store.get(ctx.params["uploadId"], ctx.bucket, ctx.key)
    number = ctx.params.get("partNumber", "")
    if not number.isdigit():
        raise S3Error("InvalidArgument", "Part number must be an integer between 1 and 10000.")
    spooled = await spool(
        ctx.request, ctx.auth, ctx.store.staging(upload.upload_id), ctx.settings.max_object_bytes
    )
    etag = await run(ctx.store.commit_part, upload, int(number), spooled)
    if sum(part.size for part in ctx.store.parts(upload)) > ctx.settings.max_object_bytes:
        raise S3Error(
            "EntityTooLarge",
            "Your proposed upload exceeds the maximum allowed size.",
            MaxSizeAllowed=str(ctx.settings.max_object_bytes),
        )
    return Response(headers={"ETag": f'"{etag}"'})


async def complete(ctx: S3Context) -> Response:
    upload = ctx.store.get(ctx.params["uploadId"], ctx.bucket, ctx.key)
    requested = s3xml.parse_complete_multipart(await read_small(ctx.request, ctx.auth, ctx.tmp_dir))
    reader = ctx.store.assemble(upload, requested, ctx.settings.max_object_bytes)
    location = str(ctx.request.url).split("?", 1)[0]

    def upload_to_filen():
        with reader:
            return ctx.upstream.put(ctx.bucket, ctx.key, reader, reader.size, None)

    task = asyncio.ensure_future(run(upload_to_filen))

    async def body():
        yield XML_DECLARATION
        while not task.done():
            await asyncio.wait({task}, timeout=KEEPALIVE_SECONDS)
            if not task.done():
                yield b" "
        try:
            etag, _ = task.result()
        except S3Error as exc:
            yield error_body(exc, location, "")
            return
        except Exception:
            log.exception("completing upload %s failed", upload.upload_id)
            yield error_body(
                S3Error("InternalError", "We encountered an internal error.", 500), location, ""
            )
            return
        # Parts are only removed once the object is safely stored, so a failed completion
        # can be retried by the client.
        await run(ctx.store.abort, upload.upload_id)
        yield s3xml.complete_multipart(location, ctx.bucket, ctx.key, etag)

    return StreamingResponse(body(), media_type="application/xml")


async def abort(ctx: S3Context) -> Response:
    upload = ctx.store.get(ctx.params["uploadId"], ctx.bucket, ctx.key)
    await run(ctx.store.abort, upload.upload_id)
    return Response(status_code=204)


async def list_parts(ctx: S3Context) -> Response:
    upload = ctx.store.get(ctx.params["uploadId"], ctx.bucket, ctx.key)
    parts = await run(ctx.store.parts, upload)
    return _xml(s3xml.list_parts(ctx.bucket, ctx.key, upload.upload_id, parts))


async def list_uploads(ctx: S3Context) -> Response:
    await run(ctx.upstream.head_bucket, ctx.bucket)
    uploads = await run(ctx.store.uploads, ctx.bucket)
    return _xml(s3xml.list_multipart_uploads(ctx.bucket, uploads))
