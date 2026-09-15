"""ListObjects semantics the gateway lacks (Delimiter, MaxKeys, pagination), computed over
a full, sorted listing."""

import base64
import binascii
from dataclasses import dataclass, field

from df_s3_filen_wrapper import Entry

from filen_s3_emulator.errors import S3Error

DEFAULT_MAX_KEYS = 1000


@dataclass
class Page:
    contents: list[Entry] = field(default_factory=list)
    common_prefixes: list[str] = field(default_factory=list)
    is_truncated: bool = False
    next_marker: str | None = None


def paginate(
    entries: list[Entry], *, prefix: str, delimiter: str, max_keys: int, after: str
) -> Page:
    """One page of ``entries`` (sorted by key) strictly after the key or prefix ``after``."""
    page = Page()
    for entry in entries:
        if not entry.key.startswith(prefix) or (after and entry.key <= after):
            continue
        common = None
        if delimiter:
            index = entry.key.find(delimiter, len(prefix))
            if index >= 0:
                common = entry.key[: index + len(delimiter)]
                # Already returned on this page, or ``after`` was this very prefix.
                if common in page.common_prefixes[-1:] or after.startswith(common):
                    continue
        if len(page.contents) + len(page.common_prefixes) >= max_keys:
            page.is_truncated = True
            break
        if common is not None:
            page.common_prefixes.append(common)
            page.next_marker = common
        else:
            page.contents.append(entry)
            page.next_marker = entry.key
    if not page.is_truncated:
        page.next_marker = None
    return page


def parse_max_keys(value: str | None) -> int:
    if value is None or value == "":
        return DEFAULT_MAX_KEYS
    if not value.isdigit():
        raise S3Error("InvalidArgument", "max-keys must be a non-negative integer.")
    return min(int(value), DEFAULT_MAX_KEYS)


def encode_token(key: str) -> str:
    return base64.urlsafe_b64encode(key.encode()).decode()


def decode_token(token: str) -> str:
    try:
        return base64.urlsafe_b64decode(token.encode()).decode()
    except (binascii.Error, UnicodeDecodeError) as exc:
        raise S3Error("InvalidArgument", "The continuation token provided is incorrect.") from exc
