"""S3 response documents, and parsing of the two XML request bodies clients send."""

from collections.abc import Iterable
from datetime import UTC, datetime
from urllib.parse import quote
from xml.etree.ElementTree import Element, SubElement, tostring

from defusedxml.ElementTree import ParseError, fromstring

from filen_s3_emulator.errors import XML_DECLARATION, S3Error

NS = "http://s3.amazonaws.com/doc/2006-03-01/"
OWNER_ID = "filen"


def iso8601(value: datetime | None) -> str:
    value = (value or datetime.now(UTC)).astimezone(UTC)
    return value.strftime("%Y-%m-%dT%H:%M:%S.") + f"{value.microsecond // 1000:03d}Z"


def render(root: Element, *, declaration: bool = True) -> bytes:
    return (XML_DECLARATION if declaration else b"") + tostring(root)


def _root(tag: str) -> Element:
    return Element(tag, xmlns=NS)


def _add(parent: Element, tag: str, text: object = None) -> Element:
    child = SubElement(parent, tag)
    if text is not None:
        child.text = str(text).lower() if isinstance(text, bool) else str(text)
    return child


def _owner(parent: Element, tag: str = "Owner") -> None:
    owner = _add(parent, tag)
    _add(owner, "ID", OWNER_ID)
    _add(owner, "DisplayName", OWNER_ID)


def quote_etag(etag: str | None) -> str | None:
    if not etag:
        return None
    return etag if etag.startswith('"') else f'"{etag}"'


def list_buckets(buckets: Iterable[tuple[str, datetime | None]]) -> bytes:
    root = _root("ListAllMyBucketsResult")
    _owner(root)
    container = _add(root, "Buckets")
    for name, created in buckets:
        bucket = _add(container, "Bucket")
        _add(bucket, "Name", name)
        _add(bucket, "CreationDate", iso8601(created))
    return render(root)


def list_objects(
    *,
    version: int,
    bucket: str,
    params: dict[str, str],
    page,
    max_keys: int,
    continuation: str | None = None,
    next_continuation: str | None = None,
) -> bytes:
    """ListBucketResult for ListObjects (``version=1``) or ListObjectsV2 (``version=2``)."""
    url = params.get("encoding-type") == "url"

    def enc(value: str) -> str:
        return quote(value, safe="/") if url else value

    root = _root("ListBucketResult")
    _add(root, "Name", bucket)
    _add(root, "Prefix", enc(params.get("prefix", "")))
    if version == 2:
        if continuation:
            _add(root, "ContinuationToken", continuation)
        if params.get("start-after"):
            _add(root, "StartAfter", enc(params["start-after"]))
        _add(root, "KeyCount", len(page.contents) + len(page.common_prefixes))
    else:
        _add(root, "Marker", enc(params.get("marker", "")))
    _add(root, "MaxKeys", max_keys)
    if params.get("delimiter"):
        _add(root, "Delimiter", enc(params["delimiter"]))
    if url:
        _add(root, "EncodingType", "url")
    _add(root, "IsTruncated", page.is_truncated)
    if page.is_truncated and version == 2 and next_continuation:
        _add(root, "NextContinuationToken", next_continuation)
    if page.is_truncated and version == 1 and page.next_marker:
        _add(root, "NextMarker", enc(page.next_marker))
    for entry in page.contents:
        item = _add(root, "Contents")
        _add(item, "Key", enc(entry.key))
        _add(item, "LastModified", iso8601(entry.last_modified))
        _add(item, "Size", entry.size)
        _add(item, "StorageClass", "STANDARD")
        if version == 1:
            _owner(item)
    for prefix in page.common_prefixes:
        _add(_add(root, "CommonPrefixes"), "Prefix", enc(prefix))
    return render(root)


def location() -> bytes:
    # An empty LocationConstraint means us-east-1; boto3 returns None for it.
    return render(_root("LocationConstraint"))


def versioning() -> bytes:
    return render(_root("VersioningConfiguration"))


def initiate_multipart(bucket: str, key: str, upload_id: str) -> bytes:
    root = _root("InitiateMultipartUploadResult")
    _add(root, "Bucket", bucket)
    _add(root, "Key", key)
    _add(root, "UploadId", upload_id)
    return render(root)


def complete_multipart(location_url: str, bucket: str, key: str, etag: str | None) -> bytes:
    root = _root("CompleteMultipartUploadResult")
    _add(root, "Location", location_url)
    _add(root, "Bucket", bucket)
    _add(root, "Key", key)
    _add(root, "ETag", quote_etag(etag) or '""')
    return render(root, declaration=False)


def list_parts(bucket: str, key: str, upload_id: str, parts) -> bytes:
    root = _root("ListPartsResult")
    _add(root, "Bucket", bucket)
    _add(root, "Key", key)
    _add(root, "UploadId", upload_id)
    _owner(root, "Initiator")
    _owner(root)
    _add(root, "StorageClass", "STANDARD")
    _add(root, "PartNumberMarker", 0)
    _add(root, "NextPartNumberMarker", parts[-1].number if parts else 0)
    _add(root, "MaxParts", 10000)
    _add(root, "IsTruncated", False)
    for part in parts:
        item = _add(root, "Part")
        _add(item, "PartNumber", part.number)
        _add(item, "LastModified", iso8601(part.last_modified))
        _add(item, "ETag", quote_etag(part.etag))
        _add(item, "Size", part.size)
    return render(root)


def list_multipart_uploads(bucket: str, uploads) -> bytes:
    root = _root("ListMultipartUploadsResult")
    _add(root, "Bucket", bucket)
    _add(root, "KeyMarker", "")
    _add(root, "UploadIdMarker", "")
    _add(root, "MaxUploads", 1000)
    _add(root, "IsTruncated", False)
    for upload in uploads:
        item = _add(root, "Upload")
        _add(item, "Key", upload.key)
        _add(item, "UploadId", upload.upload_id)
        _owner(item, "Initiator")
        _owner(item)
        _add(item, "StorageClass", "STANDARD")
        _add(item, "Initiated", iso8601(upload.initiated))
    return render(root)


def delete_result(deleted: list[str], errors: list[tuple[str, str, str]], quiet: bool) -> bytes:
    root = _root("DeleteResult")
    if not quiet:
        for key in deleted:
            _add(_add(root, "Deleted"), "Key", key)
    for key, code, message in errors:
        item = _add(root, "Error")
        _add(item, "Key", key)
        _add(item, "Code", code)
        _add(item, "Message", message)
    return render(root)


def copy_result(etag: str | None, last_modified: datetime | None) -> bytes:
    root = _root("CopyObjectResult")
    _add(root, "LastModified", iso8601(last_modified))
    _add(root, "ETag", quote_etag(etag) or '""')
    return render(root)


def post_response(location_url: str, bucket: str, key: str, etag: str | None) -> bytes:
    root = Element("PostResponse")
    _add(root, "Location", location_url)
    _add(root, "Bucket", bucket)
    _add(root, "Key", key)
    _add(root, "ETag", quote_etag(etag) or '""')
    return render(root)


# ---- request bodies -------------------------------------------------------------------


def _parse(body: bytes) -> Element:
    try:
        return fromstring(body)
    except ParseError as exc:
        raise S3Error("MalformedXML", "The XML you provided was not well-formed.") from exc


def _local(tag: str) -> str:
    return tag.rpartition("}")[2]


def _children(parent: Element, name: str) -> list[Element]:
    return [child for child in parent if _local(child.tag) == name]


def _text(parent: Element, name: str) -> str | None:
    found = _children(parent, name)
    return (found[0].text or "") if found else None


def parse_complete_multipart(body: bytes) -> list[tuple[int, str]]:
    """``[(part_number, etag)]`` in document order."""
    parts = []
    for part in _children(_parse(body), "Part"):
        number, etag = _text(part, "PartNumber"), _text(part, "ETag")
        if number is None or etag is None or not number.strip().isdigit():
            raise S3Error("MalformedXML", "Each Part needs a PartNumber and an ETag.")
        parts.append((int(number), etag.strip().strip('"')))
    if not parts:
        raise S3Error("MalformedXML", "CompleteMultipartUpload needs at least one Part.")
    return parts


def parse_delete(body: bytes) -> tuple[list[str], bool]:
    """``(keys, quiet)`` from a DeleteObjects body."""
    root = _parse(body)
    keys = [_text(item, "Key") or "" for item in _children(root, "Object")]
    if not keys or len(keys) > 1000:
        raise S3Error("MalformedXML", "Delete needs between 1 and 1000 Objects.")
    return keys, (_text(root, "Quiet") or "").strip().lower() == "true"
