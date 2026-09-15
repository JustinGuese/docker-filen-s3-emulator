"""S3 errors and their XML rendering."""

import logging
import uuid
from xml.etree.ElementTree import Element, SubElement, tostring

from fastapi import Request
from fastapi.responses import Response

log = logging.getLogger(__name__)

XML_DECLARATION = b'<?xml version="1.0" encoding="UTF-8"?>\n'


class S3Error(Exception):
    """An error with an S3 error code, rendered as ``<Error>`` XML."""

    def __init__(self, code: str, message: str, status: int = 400, **fields: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.status = status
        self.fields = fields


def error_body(exc: S3Error, resource: str, request_id: str) -> bytes:
    root = Element("Error")
    SubElement(root, "Code").text = exc.code
    SubElement(root, "Message").text = exc.message
    for name, value in exc.fields.items():
        SubElement(root, name).text = value
    SubElement(root, "Resource").text = resource
    SubElement(root, "RequestId").text = request_id
    return tostring(root)


def render_error(request: Request, exc: S3Error) -> Response:
    request_id = uuid.uuid4().hex
    headers = {"x-amz-request-id": request_id}
    if exc.status == 416 and "size" in exc.fields:
        headers["Content-Range"] = f"bytes */{exc.fields.pop('size')}"
    # An error raised before the body was fully read (e.g. key validation, which runs
    # before spooling) leaves unread bytes on the connection -- a chunked PUT body, or
    # whatever Content-Length promised. Left there, they get parsed as the start of the
    # next request on this keep-alive connection. Closing it is the only general fix.
    if request.method in ("PUT", "POST"):
        headers["Connection"] = "close"
    if request.method == "HEAD" or exc.status == 304:
        return Response(status_code=exc.status, headers=headers)
    body = XML_DECLARATION + error_body(exc, request.url.path, request_id)
    return Response(body, status_code=exc.status, media_type="application/xml", headers=headers)


async def s3_error_handler(request: Request, exc: S3Error) -> Response:
    return render_error(request, exc)


async def unhandled_error_handler(request: Request, exc: Exception) -> Response:
    log.exception("unhandled error on %s %s", request.method, request.url.path)
    return render_error(request, S3Error("InternalError", "We encountered an internal error.", 500))
