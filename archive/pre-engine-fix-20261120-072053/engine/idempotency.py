"""File and SKU idempotency utilities."""

import hashlib


def file_hash(path):
    digest = hashlib.sha256()

    with open(path, "rb") as handle:
        for chunk in iter(
            lambda: handle.read(1024 * 1024),
            b"",
        ):
            digest.update(chunk)

    return digest.hexdigest()


def record_fingerprint(record):
    import json

    payload = json.dumps(
        record,
        sort_keys=True,
        ensure_ascii=False,
    )

    return hashlib.sha256(
        payload.encode("utf-8")
    ).hexdigest()
