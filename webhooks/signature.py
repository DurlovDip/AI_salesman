"""Shared Meta webhook signature check (X-Hub-Signature-256)."""

from __future__ import annotations

import hashlib
import hmac

from config import settings


def verify_meta_signature(payload: bytes, signature_header: str) -> bool:
    """Return True when the body matches META_APP_SECRET."""
    if not signature_header:
        return False

    try:
        method, signature = signature_header.split("=", 1)
        if method != "sha256":
            return False

        expected = hmac.new(
            settings.META_APP_SECRET.encode("utf-8"),
            payload,
            hashlib.sha256,
        ).hexdigest()
        return hmac.compare_digest(expected, signature)
    except (ValueError, AttributeError):
        return False
