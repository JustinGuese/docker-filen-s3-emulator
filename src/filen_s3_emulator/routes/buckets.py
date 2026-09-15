"""ListBuckets and the bucket-level operations: create, head, delete, list, batch delete."""

import asyncio

from fastapi.responses import Response

from filen_s3_emulator import listing, s3xml
from filen_s3_emulator.body import read_small
from filen_s3_emulator.errors import S3Error
from filen_s3_emulator.routes.common import S3Context, run, validate_key, validate_prefix

DELETE_CONCURRENCY = 16


def _xml(body: bytes) -> Response:
    return Response(body, media_type="application/xml")


async def list_buckets(ctx: S3Context) -> Response:
    return _xml(s3xml.list_buckets(await run(ctx.upstream.list_buckets)))


async def create_bucket(ctx: S3Context) -> Response:
    await run(ctx.upstream.create_bucket, ctx.bucket)
    return Response(headers={"Location": f"/{ctx.bucket}"})


async def head_bucket(ctx: S3Context) -> Response:
    await run(ctx.upstream.head_bucket, ctx.bucket)
    return Response()


async def delete_bucket(ctx: S3Context) -> Response:
    await run(ctx.upstream.delete_bucket, ctx.bucket)
    return Response(status_code=204)


async def list_objects(ctx: S3Context) -> Response:
    params = ctx.params
    version = 2 if params.get("list-type") == "2" else 1
    prefix = params.get("prefix", "")
    validate_prefix(prefix)
    max_keys = listing.parse_max_keys(params.get("max-keys"))
    token = params.get("continuation-token") if version == 2 else None
    if version == 2:
        after = listing.decode_token(token) if token else params.get("start-after", "")
    else:
        after = params.get("marker", "")

    entries = await run(ctx.upstream.list, ctx.bucket, prefix)
    page = listing.paginate(
        entries,
        prefix=prefix,
        delimiter=params.get("delimiter", ""),
        max_keys=max_keys,
        after=after,
    )
    next_token = listing.encode_token(page.next_marker) if page.next_marker else None
    return _xml(
        s3xml.list_objects(
            version=version,
            bucket=ctx.bucket,
            params=params,
            page=page,
            max_keys=max_keys,
            continuation=token,
            next_continuation=next_token,
        )
    )


async def delete_objects(ctx: S3Context) -> Response:
    keys, quiet = s3xml.parse_delete(await read_small(ctx.request, ctx.auth, ctx.tmp_dir))
    limit = asyncio.Semaphore(DELETE_CONCURRENCY)

    async def delete(key: str) -> tuple[str, S3Error | None]:
        async with limit:
            try:
                validate_key(key)
                await run(ctx.upstream.delete, ctx.bucket, key)
            except S3Error as exc:
                return key, exc
            return key, None

    results = await asyncio.gather(*(delete(key) for key in keys))
    deleted = [key for key, error in results if error is None]
    errors = [(key, error.code, error.message) for key, error in results if error is not None]
    return _xml(s3xml.delete_result(deleted, errors, quiet))


async def get_location(ctx: S3Context) -> Response:
    await run(ctx.upstream.head_bucket, ctx.bucket)
    return _xml(s3xml.location())


async def get_versioning(ctx: S3Context) -> Response:
    await run(ctx.upstream.head_bucket, ctx.bucket)
    return _xml(s3xml.versioning())


def require_bucket(ctx: S3Context) -> None:
    if not ctx.bucket:
        raise S3Error("InvalidBucketName", "The specified bucket is not valid.")
