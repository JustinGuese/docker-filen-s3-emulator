"""S3 path-style routing: the operation is picked by method, path depth and sub-resource."""

from fastapi import APIRouter, Request
from fastapi.responses import Response

from filen_s3_emulator.errors import S3Error
from filen_s3_emulator.routes import buckets, multipart, objects, post
from filen_s3_emulator.routes.common import context, refuse_subresources

router = APIRouter()

BUCKET_UNSUPPORTED = frozenset(
    {
        "accelerate",
        "acl",
        "analytics",
        "cors",
        "encryption",
        "intelligent-tiering",
        "inventory",
        "lifecycle",
        "logging",
        "metrics",
        "notification",
        "object-lock",
        "ownershipControls",
        "policy",
        "policyStatus",
        "publicAccessBlock",
        "replication",
        "requestPayment",
        "tagging",
        "versions",
        "website",
    }
)
OBJECT_UNSUPPORTED = frozenset(
    {"acl", "attributes", "legal-hold", "restore", "retention", "select", "tagging", "torrent"}
)
METHODS = ["GET", "HEAD", "PUT", "POST", "DELETE"]


def _method_not_allowed() -> S3Error:
    return S3Error("MethodNotAllowed", "The specified method is not allowed.", 405)


@router.api_route("/", methods=METHODS, include_in_schema=False)
async def service(request: Request) -> Response:
    ctx = await context(request)
    if request.method != "GET":
        raise _method_not_allowed()
    return await buckets.list_buckets(ctx)


@router.api_route("/{bucket}", methods=METHODS, include_in_schema=False)
@router.api_route("/{bucket}/", methods=METHODS, include_in_schema=False)
async def bucket(request: Request, bucket: str) -> Response:
    content_type = request.headers.get("content-type", "")
    if request.method == "POST" and content_type.startswith("multipart/form-data"):
        return await post.post_object(request, bucket)

    ctx = await context(request)
    params = ctx.params
    refuse_subresources(params, BUCKET_UNSUPPORTED)
    match request.method:
        case "GET" if "location" in params:
            return await buckets.get_location(ctx)
        case "GET" if "versioning" in params:
            return await buckets.get_versioning(ctx)
        case "GET" if "uploads" in params:
            return await multipart.list_uploads(ctx)
        case "GET":
            return await buckets.list_objects(ctx)
        case "HEAD":
            return await buckets.head_bucket(ctx)
        case "PUT" if "versioning" not in params:
            return await buckets.create_bucket(ctx)
        case "DELETE":
            return await buckets.delete_bucket(ctx)
        case "POST" if "delete" in params:
            return await buckets.delete_objects(ctx)
    raise _method_not_allowed()


@router.api_route("/{bucket}/{key:path}", methods=METHODS, include_in_schema=False)
async def obj(request: Request, bucket: str, key: str) -> Response:
    ctx = await context(request)
    params = ctx.params
    refuse_subresources(params, OBJECT_UNSUPPORTED)
    match request.method:
        case "GET" if "uploadId" in params:
            return await multipart.list_parts(ctx)
        case "GET":
            return await objects.get_object(ctx, head=False)
        case "HEAD":
            return await objects.get_object(ctx, head=True)
        case "PUT" if "uploadId" in params:
            return await multipart.upload_part(ctx)
        case "PUT" if "x-amz-copy-source" in request.headers:
            return await objects.copy_object(ctx)
        case "PUT":
            return await objects.put_object(ctx)
        case "POST" if "uploads" in params:
            return await multipart.create(ctx)
        case "POST" if "uploadId" in params:
            return await multipart.complete(ctx)
        case "DELETE" if "uploadId" in params:
            return await multipart.abort(ctx)
        case "DELETE":
            return await objects.delete_object(ctx)
    raise _method_not_allowed()
