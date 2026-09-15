#!/usr/bin/env python3
"""Live smoke test against a deployed filen-s3-emulator, with a plain boto3 client.

    export FILEN_S3_EMULATOR_ENDPOINT=https://filen-s3-emulator-api.datafortress.cloud
    export FILEN_S3_EMULATOR_ACCESS_KEY=...
    export FILEN_S3_EMULATOR_SECRET_KEY=...
    export FILEN_S3_EMULATOR_BUCKET=work
    uv run python scripts/e2e.py

Writes only under a random ``_filen-s3-emulator-e2e/`` prefix and deletes everything it
created, even on failure. Confirms end to end: Cloudflare keeps the Host header and does
not normalize a signed path, keys needing percent-encoding round-trip, presigned URLs work
for a browser-like client, and multipart `upload_file` produces a byte-identical object.
"""

from __future__ import annotations

import hashlib
import os
import sys
import tempfile
import uuid
from pathlib import Path

import boto3
import httpx
from boto3.s3.transfer import TransferConfig
from botocore.config import Config
from botocore.exceptions import ClientError

ENV_ENDPOINT = "FILEN_S3_EMULATOR_ENDPOINT"
ENV_ACCESS_KEY = "FILEN_S3_EMULATOR_ACCESS_KEY"
ENV_SECRET_KEY = "FILEN_S3_EMULATOR_SECRET_KEY"
ENV_BUCKET = "FILEN_S3_EMULATOR_BUCKET"
MiB = 1024**2


def client(**config: object):
    return boto3.client(
        "s3",
        endpoint_url=os.environ[ENV_ENDPOINT],
        aws_access_key_id=os.environ[ENV_ACCESS_KEY],
        aws_secret_access_key=os.environ[ENV_SECRET_KEY],
        region_name="us-east-1",
        config=Config(**config) if config else None,
    )


def check(name: str, ok: bool, detail: str = "") -> bool:
    print(f"  {'ok' if ok else 'FAIL':4} {name}{f': {detail}' if detail and not ok else ''}")
    return ok


def main() -> int:
    bucket = os.environ[ENV_BUCKET]
    prefix = f"_filen-s3-emulator-e2e/{uuid.uuid4().hex}/"
    s3 = client()
    failures = 0

    def run(name: str, fn) -> None:
        nonlocal failures
        try:
            ok, detail = fn()
        except Exception as exc:  # noqa: BLE001 - a smoke test reports, it does not crash
            ok, detail = False, f"{type(exc).__name__}: {exc}"
        if not check(name, ok, detail):
            failures += 1

    def roundtrip_special_key():
        key = prefix + "dir/sub dir/ünï (1)+x.bin"
        data = os.urandom(4096)
        s3.put_object(Bucket=bucket, Key=key, Body=data)
        back = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
        return back == data, f"put {len(data)} bytes, got back {len(back)}"

    def head_right_after_put():
        key = prefix + "head.bin"
        s3.put_object(Bucket=bucket, Key=key, Body=b"x")
        head = s3.head_object(Bucket=bucket, Key=key)
        return head["ContentLength"] == 1, str(head["ContentLength"])

    def ranged_get():
        key = prefix + "range.bin"
        data = bytes(range(256)) * 40
        s3.put_object(Bucket=bucket, Key=key, Body=data)
        response = s3.get_object(Bucket=bucket, Key=key, Range="bytes=10000-99999")
        got = response["Body"].read()
        return got == data[10000:], response.get("ContentRange", "")

    def multipart_upload_file():
        data = os.urandom(9 * MiB)
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "source.bin"
            source.write_bytes(data)
            s3.upload_file(
                str(source),
                bucket,
                prefix + "multipart.bin",
                Config=TransferConfig(multipart_threshold=5 * MiB, multipart_chunksize=5 * MiB),
            )
        back = s3.get_object(Bucket=bucket, Key=prefix + "multipart.bin")["Body"].read()
        return hashlib.sha256(back).digest() == hashlib.sha256(data).digest(), f"{len(back)} bytes"

    def presigned_get_from_browser():
        key = prefix + "presigned.bin"
        s3.put_object(Bucket=bucket, Key=key, Body=b"browser readable")
        url = s3.generate_presigned_url(
            "get_object", Params={"Bucket": bucket, "Key": key}, ExpiresIn=60
        )
        response = httpx.get(url, headers={"User-Agent": "Mozilla/5.0"})
        return response.status_code == 200 and response.content == b"browser readable", (
            f"{response.status_code}"
        )

    def listing_pagination():
        for n in range(5):
            s3.put_object(Bucket=bucket, Key=f"{prefix}list/{n}.txt", Body=b"1")
        pages = list(
            s3.get_paginator("list_objects_v2").paginate(
                Bucket=bucket, Prefix=prefix + "list/", PaginationConfig={"PageSize": 2}
            )
        )
        keys = [o["Key"] for page in pages for o in page.get("Contents", [])]
        return len(keys) == 5 and len(pages) >= 3, f"{len(keys)} keys over {len(pages)} pages"

    def wrong_secret_is_rejected():
        bad = client()
        bad._request_signer._credentials.secret_key = "not-the-real-secret"
        try:
            bad.list_buckets()
        except ClientError as exc:
            return exc.response["Error"]["Code"] == "SignatureDoesNotMatch", str(exc)
        return False, "list_buckets succeeded with a wrong secret"

    print(f"endpoint  {os.environ[ENV_ENDPOINT]}")
    print(f"bucket    {bucket}")
    print(f"prefix    {prefix}\n")

    try:
        run("byte round-trip with a key needing percent-encoding", roundtrip_special_key)
        run("HEAD right after PUT", head_right_after_put)
        run("ranged GET", ranged_get)
        run("multipart upload_file, byte-identical", multipart_upload_file)
        run("presigned GET, browser User-Agent", presigned_get_from_browser)
        run("list_objects_v2 pagination", listing_pagination)
        run("wrong secret is rejected", wrong_secret_is_rejected)
    finally:
        deleted = 0
        token = None
        while True:
            kwargs = {"Bucket": bucket, "Prefix": prefix}
            if token:
                kwargs["ContinuationToken"] = token
            page = s3.list_objects_v2(**kwargs)
            keys = [{"Key": o["Key"]} for o in page.get("Contents", [])]
            if keys:
                s3.delete_objects(Bucket=bucket, Delete={"Objects": keys})
                deleted += len(keys)
            token = page.get("NextContinuationToken")
            if not page.get("IsTruncated"):
                break
        print(f"\ncleaned up {deleted} objects under {prefix}")

    print(f"\n{'FAILED' if failures else 'all checks passed'} ({failures} failing)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
