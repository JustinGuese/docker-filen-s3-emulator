# filen-s3-emulator

An S3-compatible front for the [Filen CLI's S3 gateway](https://github.com/FilenCloudDienste/filen-s3)
(`filen s3`), so a **plain, unconfigured `boto3.client("s3", endpoint_url=...)`** can use a
Filen bucket the way it would use real S3 -- default checksums, presigned URLs, multipart
`upload_file`, `download_file`, delimited/paginated listings, and a HEAD right after a PUT.

```
boto3 client  →  Cloudflare tunnel  →  this service  →  filen s3 gateway (in-cluster)
```

Self-contained: the Helm chart in [`helm/filen-s3-emulator`](helm/filen-s3-emulator) deploys
the gateway itself (the Filen CLI's `filen s3`, image `filen/cli`) alongside this service,
reached over its ClusterIP Service -- one `helm install` brings up both. The gateway is not
exposed outside the cluster; only this service is, through the chart's Ingress.

The gateway only implements part of S3 -- no multipart, `Range` past the object size is a
400, `ListObjectsV2` without `Prefix` is a 400, HEAD answers 401 for about a second after a
PUT, and more (see [`df-s3-filen-wrapper`](https://github.com/JustinGuese/df-s3-filen-wrapper),
which this service is built on for the actual gateway calls). This service closes those gaps
and adds request authentication, so the _client_ never has to know any of it.

## Supported

| Operation                                                                    | Notes                                                                                                                                                                                                                                                                                                                              |
| ---------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `list_buckets`, `create_bucket`, `head_bucket`, `delete_bucket`              |                                                                                                                                                                                                                                                                                                                                    |
| `put_object`, `get_object`, `head_object`, `delete_object`, `delete_objects` |                                                                                                                                                                                                                                                                                                                                    |
| `copy_object`                                                                | Same-bucket copy-to-self needs `MetadataDirective=REPLACE` (the gateway deletes the destination first, which here is the source).                                                                                                                                                                                                  |
| `list_objects` / `list_objects_v2`                                           | `Prefix`, `Delimiter`, `MaxKeys` (capped at 1000), `Marker`/`StartAfter`/`ContinuationToken`. A listing walks at most 10 directory levels below the prefix -- a gateway limit.                                                                                                                                                     |
| Multipart upload                                                             | `create_multipart_upload`, `upload_part`, `complete_multipart_upload`, `abort_multipart_upload`, `list_parts`, `list_multipart_uploads`. Parts are staged on local disk and completed as **one** upstream PUT (the gateway has no native multipart), capped by `MAX_OBJECT_BYTES` -- see below. `UploadPartCopy` is not supported. |
| Presigned URLs                                                               | `generate_presigned_url` for GET and PUT, both SigV2 (boto3's default) and SigV4 (`Config(signature_version="s3v4")`).                                                                                                                                                                                                             |
| Presigned POST                                                               | `generate_presigned_post` -- browser form uploads, `${filename}`, `eq`/`starts-with`/`content-length-range` policy conditions.                                                                                                                                                                                                     |
| Conditional requests                                                         | `If-Match`, `If-None-Match`, `If-Modified-Since`, `If-Unmodified-Since` on GET/HEAD/PUT.                                                                                                                                                                                                                                           |
| Ranged GET, `download_file`                                                  | Ranges are resolved to in-bounds `bytes=a-b` before they reach the gateway, which 400s on anything else.                                                                                                                                                                                                                           |

## Not supported

Sent back as `501 NotImplemented` (bucket/object ACLs, tagging, versioning, lifecycle,
CORS, encryption, object lock, `UploadPartCopy`, ...) or simply not stored:

- **Object metadata (`x-amz-meta-*`) and a stored Content-Type are not kept.** The gateway
  accepts them on PUT and never returns them. Keep that information in your own database.
- **No real ETag guarantee across all paths.** The gateway's ETag is the object's UUID, not
  an MD5; it is still stable and suitable for `If-Match`/`If-None-Match`.

## Multipart uploads and the size cap

There is no database and no multipart support on the gateway. A multipart upload is staged
part-by-part on local disk (`STAGING_DIR`, an `emptyDir` volume in the cluster) and, on
`CompleteMultipartUpload`, concatenated into **one** upstream PUT -- which the gateway
buffers entirely in memory. `MAX_OBJECT_BYTES` (default 1 GiB) is enforced on every PUT,
staged part, and completed upload; measure the gateway pod's actual memory headroom before
raising it. Abandoned uploads and orphaned spool files are swept hourly, after
`MULTIPART_EXPIRY_HOURS` (default 24) of inactivity.

Because `CompleteMultipartUpload` can take longer than Cloudflare's ~100s time-to-first-byte
limit, this service answers `200` immediately and keeps the connection open with whitespace
until the result (or an in-body `<Error>`) is ready -- the same trick real S3 uses, and one
botocore already knows how to handle for this operation.

## Configuration

Locally, layered `.env` files (see [ske](https://github.com/JustinGuese/ske-simple-kubernetes-environments),
which generated this project's scaffolding):

```
.env             public defaults          committed
.env.local       local overrides          committed
.env.secret      credentials, plaintext   GITIGNORED
```

| Variable                                    |                                                                                                                                                                                                        |
| ------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `S3_ACCESS_KEY_ID` / `S3_SECRET_ACCESS_KEY` | The one key pair: clients of this service sign with it, and the Filen gateway is started with it. |
| `FILEN_ENDPOINT` | Where the gateway is reached -- the chart's sidecar on loopback (`http://127.0.0.1:8080`, set automatically), or a gateway you run locally (`.env.local`). |
| `FILEN_EMAIL` / `FILEN_PASSWORD`            | The Filen account the bundled gateway logs into.                                                                                                                                                       |
| `MAX_OBJECT_BYTES`                          | Single-PUT / completed-multipart-upload cap (default 1 GiB).                                                                                                                                           |
| `STAGING_DIR`                               | Where multipart parts and in-flight bodies are spooled (default `/staging`).                                                                                                                           |
| `MULTIPART_EXPIRY_HOURS`                    | How long an abandoned multipart upload survives before the hourly sweep removes it (default 24).                                                                                                       |

In the cluster, the non-secret variables above come from the Helm chart's `values.yaml`
(`env:` map) and the credentials come from a Secret created by hand -- **never from
`values.yaml`, and never committed.** See "Deploy with Helm" below.

## Local development

    uv sync
    # the gateway, with the same key pair (FILEN_EMAIL / FILEN_PASSWORD from .env.secret)
    set -a; . ./.env.secret; set +a
    npx @filen/cli --skip-update s3 --s3-hostname 127.0.0.1 --s3-port 8080 \
      --s3-access-key-id "$S3_ACCESS_KEY_ID" --s3-secret-access-key "$S3_SECRET_ACCESS_KEY"
    # in another shell
    uv run uvicorn filen_s3_emulator.main:app --reload
    uv run pytest -q
    uv run ruff check . && uv run ruff format --check .

The test suite is entirely offline: a real uvicorn on `127.0.0.1` stands in for the
deployed service, a plain boto3 client drives it exactly as a real caller would, and an
in-memory `FakeUpstream` stands in for the gateway. Request signatures are checked against
what real botocore actually signs, captured with its own `before-send` hook -- never
hand-assembled.

## CI: image build

`.github/workflows/docker-publish.yml` runs the test suite (`ruff` + `pytest`) and, on
`main`, builds and pushes the image to Docker Hub as `guestros/filen-s3-emulator:latest`
and `:<commit-sha>`. It needs two repo secrets under **Settings -> Secrets and variables ->
Actions**:

| Secret               |                                                                                                                                       |
| -------------------- | ------------------------------------------------------------------------------------------------------------------------------------- |
| `DOCKERHUB_USERNAME` | Docker Hub username (`guestros`).                                                                                                     |
| `DOCKERHUB_TOKEN`    | A Docker Hub [access token](https://app.docker.com/settings/personal-access-tokens) (not the account password) with read/write scope. |

CI never sees Filen or cluster credentials -- it only builds and pushes the image.

## Deploy with Helm

The chart lives at [`helm/filen-s3-emulator`](helm/filen-s3-emulator) and deploys both
this service and the bundled `filen s3` gateway. **Filen and cluster credentials never go
into `values.yaml`** -- they're created once as a plain Kubernetes Secret, from the same
`.env.secret` file used locally:

```
S3_ACCESS_KEY_ID / S3_SECRET_ACCESS_KEY   the one key pair: clients sign with it, the gateway is started with it
FILEN_EMAIL / FILEN_PASSWORD              the Filen account the gateway logs into
```

Copy `.env.secret.example` to `.env.secret` and fill in real values, then:

    kubectl create namespace filen-s3-emulator
    kubectl create secret generic filen-s3-emulator-secrets \
      --from-env-file=.env.secret \
      --namespace filen-s3-emulator

Re-running that `create secret` command after editing `.env.secret` fails because the
Secret already exists -- use `--dry-run=client -o yaml | kubectl apply -f -` instead when
rotating a value, then `kubectl -n filen-s3-emulator rollout restart deploy/filen-s3-emulator`
(a plain Secret isn't content-hashed, so pods don't roll on their own).

Then install or upgrade the chart:

    helm upgrade --install filen-s3-emulator helm/filen-s3-emulator \
      --namespace filen-s3-emulator

The chart assumes the Secret above already exists under the name in `values.yaml`'s
`secretName` (default `filen-s3-emulator-secrets`); `helm install` still succeeds if it
doesn't, but the pods sit in `CreateContainerConfigError` until it does -- `helm install`
prints a warning to that effect via `NOTES.txt`.

Everything else -- image tag, resource limits, the Ingress hostname, `MAX_OBJECT_BYTES`,
whether the bundled gateway is enabled -- is a `values.yaml` override:

    helm upgrade --install filen-s3-emulator helm/filen-s3-emulator \
      --namespace filen-s3-emulator \
      --set image.tag=abc1234 \
      --set ingress.host=my-host.example.com

`values.yaml`'s `ingress.className: cloudflare-tunnel` fronts the service through a
Cloudflare Tunnel Ingress class. **A default Python `User-Agent` gets blocked by Cloudflare
before it reaches this service** -- harmless for boto3 (which sends its own), but relevant
if you write a plain `urllib`/`curl` client against a presigned URL.

To point the chart at a gateway that's already running elsewhere instead of deploying its
own, set `filenCli.enabled=false` and `env.FILEN_ENDPOINT` to that gateway's address.

## After deploying

`scripts/e2e.py` drives the deployed service with a plain boto3 client -- percent-encoded
keys, HEAD right after PUT, ranges, multipart `upload_file`, presigned GET, listing
pagination -- and cleans up everything it wrote:

    export FILEN_S3_EMULATOR_ENDPOINT=https://filen-s3-emulator-api.datafortress.cloud
    export FILEN_S3_EMULATOR_ACCESS_KEY=...      # this service's S3_ACCESS_KEY_ID
    export FILEN_S3_EMULATOR_SECRET_KEY=...      # this service's S3_SECRET_ACCESS_KEY
    export FILEN_S3_EMULATOR_BUCKET=work
    uv run python scripts/e2e.py
