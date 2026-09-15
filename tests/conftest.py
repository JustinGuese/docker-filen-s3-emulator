"""Env vars are ASSIGNED, not setdefault -- the suite must never inherit the developer's own
.env, and must never run with live Filen credentials loaded."""

import os
import tempfile

os.environ["APP_ENV"] = "test"
os.environ["S3_ACCESS_KEY_ID"] = "test-access-key"
os.environ["S3_SECRET_ACCESS_KEY"] = "test-secret-key"
os.environ["FILEN_ENDPOINT"] = "http://filen.invalid"
os.environ["FILEN_ACCESS_KEY"] = "filen-access"
os.environ["FILEN_SECRET_KEY"] = "filen-secret"
os.environ["STAGING_DIR"] = tempfile.mkdtemp(prefix="filen-s3-emulator-test-")
for name in list(os.environ):
    if name.startswith("AWS_"):
        del os.environ[name]
