"""PostObject: browser form uploads made from ``generate_presigned_post``."""

from datetime import UTC, datetime
from urllib.parse import quote, urlencode

from fastapi import Request
from fastapi.responses import RedirectResponse, Response
from starlette.datastructures import UploadFile

from filen_s3_emulator import s3xml
from filen_s3_emulator.auth import post_policy
from filen_s3_emulator.errors import S3Error
from filen_s3_emulator.routes.common import run, validate_key


async def post_object(request: Request, bucket: str) -> Response:
    settings, upstream = request.app.state.settings, request.app.state.upstream
    form = await request.form(max_files=1, max_fields=64)
    try:
        fields: dict[str, str] = {}
        upload: UploadFile | None = None
        for name, value in form.multi_items():
            if isinstance(value, UploadFile):
                if name.lower() == "file":
                    upload = value
            else:
                fields[name.lower()] = value
        if upload is None:
            raise S3Error("InvalidArgument", "POST requires exactly one file upload per request.")

        policy = post_policy.verify(
            fields, settings.s3_access_key_id, settings.s3_secret_access_key, datetime.now(UTC)
        )
        key = fields.get("key", "").replace("${filename}", upload.filename or "")
        fields["key"] = key
        validate_key(key)
        size = upload.size if upload.size is not None else upload.file.seek(0, 2)
        upload.file.seek(0)
        post_policy.check_conditions(policy, fields, bucket, size)
        if size > settings.max_object_bytes:
            raise S3Error(
                "EntityTooLarge",
                "Your proposed upload exceeds the maximum allowed size.",
                MaxSizeAllowed=str(settings.max_object_bytes),
            )
        etag, _ = await run(
            upstream.put, bucket, key, upload.file, size, fields.get("content-type")
        )
    finally:
        await form.close()

    location = f"{str(request.base_url).rstrip('/')}/{bucket}/{quote(key)}"
    quoted_etag = s3xml.quote_etag(etag) or '""'
    if redirect := fields.get("success_action_redirect") or fields.get("redirect"):
        separator = "&" if "?" in redirect else "?"
        query = urlencode({"bucket": bucket, "key": key, "etag": quoted_etag})
        return RedirectResponse(f"{redirect}{separator}{query}", status_code=303)
    status = fields.get("success_action_status", "204")
    if status == "201":
        return Response(
            s3xml.post_response(location, bucket, key, etag),
            status_code=201,
            media_type="application/xml",
            headers={"ETag": quoted_etag, "Location": location},
        )
    return Response(
        status_code=200 if status == "200" else 204,
        headers={"ETag": quoted_etag, "Location": location},
    )
