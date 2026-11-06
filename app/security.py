import hashlib
import hmac


def hash_secret(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def verify_secret(value: str | None, expected_hash: str) -> bool:
    if not value:
        return False
    return hmac.compare_digest(hash_secret(value), expected_hash)


def event_fingerprint(repository_key: str, raw_body: bytes) -> str:
    digest = hashlib.sha256(repository_key.encode() + b"\0" + raw_body).hexdigest()
    return f"generated-{digest}"
