"""Browser form uploads (``generate_presigned_post``): the signed policy and its conditions."""

import base64
import binascii
import hmac
import json
from datetime import UTC, datetime

from filen_s3_emulator.auth import sigv2, sigv4
from filen_s3_emulator.auth.common import check_access_key, check_signature, hmac_hex
from filen_s3_emulator.errors import S3Error


def _denied(message: str) -> S3Error:
    return S3Error("AccessDenied", f"Invalid according to Policy: {message}", 403)


def verify(fields: dict[str, str], access_key: str, secret: str, now: datetime) -> dict:
    """Check the policy signature and expiry. ``fields`` has lower-cased names."""
    policy_b64 = fields.get("policy")
    if not policy_b64:
        raise S3Error("AccessDenied", "Anonymous form uploads are not allowed.", 403)

    if "x-amz-signature" in fields:
        if fields.get("x-amz-algorithm") != sigv4.ALGORITHM:
            raise S3Error("InvalidArgument", "x-amz-algorithm must be AWS4-HMAC-SHA256.")
        parts = fields.get("x-amz-credential", "").split("/")
        if len(parts) != 5:
            raise S3Error("InvalidArgument", "Malformed x-amz-credential.")
        check_access_key(parts[0], access_key)
        key = sigv4.signing_key(secret, parts[1], parts[2], parts[3])
        check_signature(fields["x-amz-signature"], hmac_hex(key, policy_b64))
    elif "signature" in fields:
        check_access_key(fields.get("awsaccesskeyid", ""), access_key)
        check_signature(fields["signature"], sigv2.sign(secret, policy_b64))
    else:
        raise S3Error("AccessDenied", "Form upload is missing its signature.", 403)

    try:
        policy = json.loads(base64.b64decode(policy_b64))
        expiration = datetime.fromisoformat(policy["expiration"].replace("Z", "+00:00"))
    except (ValueError, KeyError, TypeError, binascii.Error) as exc:
        raise S3Error("InvalidPolicyDocument", "Invalid Policy: malformed policy.") from exc
    if expiration.tzinfo is None:
        expiration = expiration.replace(tzinfo=UTC)
    if now > expiration:
        raise _denied("Policy expired.")
    return policy


def check_conditions(policy: dict, fields: dict[str, str], bucket: str, size: int) -> None:
    """Every condition must hold. ``key`` in ``fields`` is already ``${filename}``-expanded."""

    def actual(name: str) -> str:
        name = name.lstrip("$").lower()
        return bucket if name == "bucket" else fields.get(name, "")

    for condition in policy.get("conditions", []):
        if isinstance(condition, dict):
            for name, expected in condition.items():
                if not hmac.compare_digest(actual(name).encode(), str(expected).encode()):
                    raise _denied(f'Policy Condition failed: ["eq", "${name}", "{expected}"]')
            continue
        if not isinstance(condition, list) or len(condition) != 3:
            raise S3Error("InvalidPolicyDocument", "Invalid Policy: malformed condition.")
        operator = str(condition[0]).lower()
        if operator == "content-length-range":
            low, high = int(condition[1]), int(condition[2])
            if not low <= size <= high:
                raise S3Error(
                    "EntityTooLarge" if size > high else "EntityTooSmall",
                    "Your proposed upload does not match the content-length-range.",
                )
        elif operator == "eq":
            if actual(condition[1]) != str(condition[2]):
                raise _denied(f"Policy Condition failed: {condition}")
        elif operator == "starts-with":
            if not actual(condition[1]).startswith(str(condition[2])):
                raise _denied(f"Policy Condition failed: {condition}")
        else:
            raise S3Error("InvalidPolicyDocument", f"Invalid Policy: unknown operator {operator}.")
