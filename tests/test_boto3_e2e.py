"""A plain, unconfigured boto3 client against the service, over real HTTP."""

import hashlib
import os
import time

import boto3
import httpx
import pytest
from boto3.s3.transfer import TransferConfig
from botocore.config import Config
from botocore.exceptions import ClientError
from fake_upstream import FakeUpstream
from server import serve

from filen_s3_emulator.config import Settings

KEY, SECRET = "test-access-key", "test-secret-key"
MiB = 1024**2


@pytest.fixture(scope="module")
def upstream():
    return FakeUpstream()


@pytest.fixture(scope="module")
def endpoint(upstream, tmp_path_factory):
    settings = Settings(
        s3_access_key_id=KEY,
        s3_secret_access_key=SECRET,
        staging_dir=str(tmp_path_factory.mktemp("staging")),
        max_object_bytes=40 * MiB,
    )
    with serve(settings, upstream) as url:
        yield url


def client(endpoint, **config):
    return boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=KEY,
        aws_secret_access_key=SECRET,
        region_name="us-east-1",
        config=Config(**config) if config else None,
    )


@pytest.fixture(scope="module")
def s3(endpoint):
    return client(endpoint)


def test_put_get_head_with_default_checksums(s3, upstream):
    data = os.urandom(100_000)
    key = "dir/sub dir/ünï (1)+x.bin"
    put = s3.put_object(Bucket="test", Key=key, Body=data)
    # Stored exactly: the aws-chunked framing and CRC32 trailer were decoded.
    assert upstream.buckets["test"][key][0] == data
    assert put["ETag"].startswith('"')

    head = s3.head_object(Bucket="test", Key=key)
    assert head["ContentLength"] == len(data)
    assert head["ETag"] == put["ETag"]
    assert s3.get_object(Bucket="test", Key=key)["Body"].read() == data


def test_ranges(s3):
    data = bytes(range(256)) * 40
    s3.put_object(Bucket="test", Key="range.bin", Body=data)

    def get(spec):
        response = s3.get_object(Bucket="test", Key="range.bin", Range=spec)
        return response["Body"].read(), response.get("ContentRange")

    assert get("bytes=10-19") == (data[10:20], "bytes 10-19/10240")
    assert get("bytes=10000-99999") == (data[10000:], "bytes 10000-10239/10240")
    assert get("bytes=-100") == (data[-100:], "bytes 10140-10239/10240")
    assert get("bytes=0-")[0] == data
    with pytest.raises(ClientError) as exc:
        get("bytes=20000-")
    assert exc.value.response["Error"]["Code"] == "InvalidRange"


def test_download_file_and_multipart_upload_file(s3, upstream, tmp_path):
    data = os.urandom(12 * MiB + 123)
    source = tmp_path / "source.bin"
    source.write_bytes(data)
    config = TransferConfig(multipart_threshold=5 * MiB, multipart_chunksize=5 * MiB)
    s3.upload_file(str(source), "test", "big/file.bin", Config=config)
    assert hashlib.sha256(upstream.buckets["test"]["big/file.bin"][0]).digest() == (
        hashlib.sha256(data).digest()
    )

    target = tmp_path / "target.bin"
    s3.download_file("test", "big/file.bin", str(target), Config=config)
    assert target.read_bytes() == data

    small = tmp_path / "small.bin"
    s3.download_file("test", "range.bin", str(small))
    assert small.stat().st_size == 10240


def test_multipart_list_parts_and_abort(s3):
    upload = s3.create_multipart_upload(Bucket="test", Key="aborted.bin")
    upload_id = upload["UploadId"]
    part = s3.upload_part(
        Bucket="test", Key="aborted.bin", UploadId=upload_id, PartNumber=1, Body=b"x" * 1000
    )
    assert part["ETag"] == f'"{hashlib.md5(b"x" * 1000).hexdigest()}"'
    parts = s3.list_parts(Bucket="test", Key="aborted.bin", UploadId=upload_id)["Parts"]
    assert [(p["PartNumber"], p["Size"]) for p in parts] == [(1, 1000)]
    uploads = s3.list_multipart_uploads(Bucket="test").get("Uploads", [])
    assert upload_id in [u["UploadId"] for u in uploads]

    s3.abort_multipart_upload(Bucket="test", Key="aborted.bin", UploadId=upload_id)
    with pytest.raises(ClientError) as exc:
        s3.list_parts(Bucket="test", Key="aborted.bin", UploadId=upload_id)
    assert exc.value.response["Error"]["Code"] == "NoSuchUpload"


def test_small_non_last_part_is_refused(s3):
    upload_id = s3.create_multipart_upload(Bucket="test", Key="tiny.bin")["UploadId"]
    etags = [
        s3.upload_part(
            Bucket="test", Key="tiny.bin", UploadId=upload_id, PartNumber=n, Body=b"y" * 10
        )["ETag"]
        for n in (1, 2)
    ]
    with pytest.raises(ClientError) as exc:
        s3.complete_multipart_upload(
            Bucket="test",
            Key="tiny.bin",
            UploadId=upload_id,
            MultipartUpload={
                "Parts": [{"PartNumber": n, "ETag": e} for n, e in zip((1, 2), etags, strict=True)]
            },
        )
    assert exc.value.response["Error"]["Code"] == "EntityTooSmall"


def test_listing_pagination_and_delimiter(s3):
    for key in ["list/a.txt", "list/b/1.txt", "list/b/2.txt", "list/c/3.txt", "list/d.txt"]:
        s3.put_object(Bucket="test", Key=key, Body=b"1")

    pages = s3.get_paginator("list_objects_v2").paginate(
        Bucket="test", Prefix="list/", Delimiter="/", PaginationConfig={"PageSize": 2}
    )
    keys, prefixes = [], []
    for page in pages:
        keys += [o["Key"] for o in page.get("Contents", [])]
        prefixes += [p["Prefix"] for p in page.get("CommonPrefixes", [])]
    assert keys == ["list/a.txt", "list/d.txt"]
    assert prefixes == ["list/b/", "list/c/"]

    flat = [
        o["Key"]
        for p in s3.get_paginator("list_objects").paginate(
            Bucket="test", Prefix="list/", PaginationConfig={"PageSize": 2}
        )
        for o in p["Contents"]
    ]
    assert flat == ["list/a.txt", "list/b/1.txt", "list/b/2.txt", "list/c/3.txt", "list/d.txt"]

    whole = s3.list_objects_v2(Bucket="test")
    assert whole["KeyCount"] > 0


def test_copy_and_delete_objects(s3, upstream):
    s3.put_object(Bucket="test", Key="copy/src.txt", Body=b"hello")
    s3.copy_object(
        Bucket="test", Key="copy/dst.txt", CopySource={"Bucket": "test", "Key": "copy/src.txt"}
    )
    assert s3.get_object(Bucket="test", Key="copy/dst.txt")["Body"].read() == b"hello"

    with pytest.raises(ClientError) as exc:
        s3.copy_object(
            Bucket="test", Key="copy/src.txt", CopySource={"Bucket": "test", "Key": "copy/src.txt"}
        )
    assert exc.value.response["Error"]["Code"] == "InvalidRequest"
    assert "copy/src.txt" in upstream.buckets["test"]

    result = s3.delete_objects(
        Bucket="test",
        Delete={"Objects": [{"Key": "copy/src.txt"}, {"Key": "copy/dst.txt"}, {"Key": "bad//key"}]},
    )
    assert sorted(d["Key"] for d in result["Deleted"]) == ["copy/dst.txt", "copy/src.txt"]
    assert [e["Key"] for e in result["Errors"]] == ["bad//key"]
    s3.delete_object(Bucket="test", Key="copy/never-existed.txt")


def test_presigned_urls(endpoint, s3):
    s3.put_object(Bucket="test", Key="pre signed/ä.txt", Body=b"browser")
    browser = {"User-Agent": "Mozilla/5.0"}
    for signer in (s3, client(endpoint, signature_version="s3v4")):
        url = signer.generate_presigned_url(
            "get_object", Params={"Bucket": "test", "Key": "pre signed/ä.txt"}, ExpiresIn=60
        )
        response = httpx.get(url, headers=browser)
        assert response.status_code == 200, response.text
        assert response.content == b"browser"

        tampered = url.replace("pre%20signed", "pre%20signeD")
        assert httpx.get(tampered).status_code == 403

        put_url = signer.generate_presigned_url(
            "put_object", Params={"Bucket": "test", "Key": "pre signed/put.txt"}, ExpiresIn=60
        )
        assert httpx.put(put_url, content=b"uploaded").status_code == 200
        assert s3.get_object(Bucket="test", Key="pre signed/put.txt")["Body"].read() == b"uploaded"


def test_presigned_url_expires(s3):
    # A wide margin: a short real sleep close to ExpiresIn is flaky under wall-clock
    # jitter (observed under WSL2, where a nominal 2s sleep once only advanced the clock
    # by ~0.6s) -- the exact expiry boundary is covered deterministically in test_auth.py.
    url = s3.generate_presigned_url(
        "get_object", Params={"Bucket": "test", "Key": "x"}, ExpiresIn=1
    )
    time.sleep(5)
    response = httpx.get(url)
    assert response.status_code == 403
    assert b"expired" in response.content


@pytest.mark.parametrize("version", [None, "s3v4"])
def test_presigned_post(endpoint, upstream, version):
    signer = client(endpoint, signature_version=version) if version else client(endpoint)
    post = signer.generate_presigned_post(
        "test",
        "forms/${filename}",
        Conditions=[["starts-with", "$key", "forms/"], ["content-length-range", 1, 1000]],
        ExpiresIn=60,
    )
    response = httpx.post(
        post["url"], data=post["fields"], files={"file": ("hello.txt", b"form upload")}
    )
    assert response.status_code == 204, response.text
    assert upstream.buckets["test"]["forms/hello.txt"][0] == b"form upload"

    too_big = httpx.post(post["url"], data=post["fields"], files={"file": ("big.txt", b"z" * 2000)})
    assert too_big.status_code == 400


def test_auth_failures(endpoint):
    wrong = boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=KEY,
        aws_secret_access_key="nope",
        region_name="us-east-1",
    )
    with pytest.raises(ClientError) as exc:
        wrong.list_buckets()
    assert exc.value.response["Error"]["Code"] == "SignatureDoesNotMatch"
    assert httpx.get(f"{endpoint}/test?list-type=2").status_code == 403


def test_oversize_and_unstorable_keys(s3):
    with pytest.raises(ClientError) as exc:
        s3.put_object(Bucket="test", Key="huge.bin", Body=b"0" * (41 * MiB))
    assert exc.value.response["Error"]["Code"] == "EntityTooLarge"
    for key in ["a%20b", " lead", "a//b", "a/../b"]:
        with pytest.raises(ClientError) as exc:
            s3.put_object(Bucket="test", Key=key, Body=b"1")
        assert exc.value.response["Error"]["Code"] == "InvalidArgument", key


def test_buckets_and_subresources(s3):
    assert "test" in [b["Name"] for b in s3.list_buckets()["Buckets"]]
    s3.create_bucket(Bucket="made")
    s3.head_bucket(Bucket="made")
    assert s3.get_bucket_location(Bucket="made")["LocationConstraint"] is None
    s3.delete_bucket(Bucket="made")
    with pytest.raises(ClientError) as exc:
        s3.head_bucket(Bucket="made")
    assert exc.value.response["ResponseMetadata"]["HTTPStatusCode"] == 404
    with pytest.raises(ClientError) as exc:
        s3.get_bucket_acl(Bucket="test")
    assert exc.value.response["Error"]["Code"] == "NotImplemented"


def test_complete_failure_is_reported_and_retryable(s3, upstream):
    upload_id = s3.create_multipart_upload(Bucket="test", Key="retry.bin")["UploadId"]
    etag = s3.upload_part(
        Bucket="test", Key="retry.bin", UploadId=upload_id, PartNumber=1, Body=b"r" * 100
    )["ETag"]
    parts = {"Parts": [{"PartNumber": 1, "ETag": etag}]}
    upstream.fail_puts = True
    no_retry = client(s3.meta.endpoint_url, retries={"max_attempts": 1})
    try:
        with pytest.raises(ClientError):
            no_retry.complete_multipart_upload(
                Bucket="test", Key="retry.bin", UploadId=upload_id, MultipartUpload=parts
            )
    finally:
        upstream.fail_puts = False
    s3.complete_multipart_upload(
        Bucket="test", Key="retry.bin", UploadId=upload_id, MultipartUpload=parts
    )
    assert upstream.buckets["test"]["retry.bin"][0] == b"r" * 100
