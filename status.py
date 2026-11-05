"""
Honest process status for /health.

Meta tokens and AI keys are reported separately. A Page token can deliver
a webhook, but it cannot generate a reply.
"""

from __future__ import annotations

from typing import Any, Dict, List

from config import FABINGO_APP_ID, FABINGO_PAGE_ID, settings


def _channel(
    *,
    enabled: bool,
    configured: bool,
    missing_tokens: List[str],
    needs_ai: bool,
) -> Dict[str, Any]:
    blocking: List[str] = []
    if not enabled:
        blocking.append("disabled by env flag")
    else:
        if missing_tokens:
            blocking.append("missing Meta token: " + ", ".join(missing_tokens))
        if needs_ai and not settings.ai_reply_ready:
            blocking.append("missing AI key")
    ready = enabled and configured and (settings.ai_reply_ready or not needs_ai)
    # Reactions can run without an AI key (a like is not a generated reply).
    if needs_ai and not settings.ai_reply_ready:
        ready = False
    return {
        "enabled": enabled,
        "token_configured": configured,
        "ready": ready,
        "missing_tokens": missing_tokens if enabled else [],
        "blocking": blocking,
    }


def health_snapshot(active_conversations: int = 0) -> Dict[str, Any]:
    """Build the /health payload. Does not include secrets."""
    messenger_missing = [] if settings.META_PAGE_ACCESS_TOKEN else ["META_PAGE_ACCESS_TOKEN"]
    whatsapp_missing = []
    if not settings.META_WHATSAPP_TOKEN:
        whatsapp_missing.append("META_WHATSAPP_TOKEN")
    if not settings.META_WHATSAPP_PHONE_ID:
        whatsapp_missing.append("META_WHATSAPP_PHONE_ID")
    instagram_missing = [] if settings.META_PAGE_ACCESS_TOKEN else ["META_PAGE_ACCESS_TOKEN"]
    page_missing = list(messenger_missing)

    channels = {
        "messenger": _channel(
            enabled=settings.messenger_enabled,
            configured=settings.messenger_configured,
            missing_tokens=messenger_missing,
            needs_ai=True,
        ),
        "whatsapp": _channel(
            enabled=settings.whatsapp_enabled,
            configured=settings.whatsapp_configured,
            missing_tokens=whatsapp_missing,
            needs_ai=True,
        ),
        "instagram": _channel(
            enabled=settings.instagram_enabled,
            configured=settings.instagram_configured,
            missing_tokens=instagram_missing,
            needs_ai=True,
        ),
        "page_comments": _channel(
            enabled=settings.page_comments_enabled,
            configured=settings.page_engagement_configured,
            missing_tokens=page_missing,
            needs_ai=True,
        ),
        "page_reactions": _channel(
            enabled=settings.page_reactions_enabled,
            configured=settings.page_engagement_configured,
            missing_tokens=page_missing,
            needs_ai=False,
        ),
    }

    reasons: List[str] = []
    if not settings.ai_reply_ready:
        reasons.append(
            "No AI provider is configured. Set GEMINI_API_KEY, OPENAI_API_KEY, "
            "or GROQ_API_KEY (or an explicit MULTI_AI_API_URL). "
            "Meta tokens do not generate replies."
        )
    for name, channel in channels.items():
        if channel["enabled"] and channel["missing_tokens"]:
            reasons.append(
                f"{name} is enabled but missing {', '.join(channel['missing_tokens'])}."
            )

    return {
        "status": "ok" if not reasons else "degraded",
        "reasons": reasons,
        "environment": "local" if settings.DEBUG else "production",
        "polling_loop_enabled": settings.messenger_polling_enabled and settings.messenger_enabled,
        "webhooks": {
            "messenger": "/webhook/messenger",
            "whatsapp": "/webhook/whatsapp",
            "instagram": "/webhook/instagram",
            "page": "/webhook/page",
        },
        "fabingo": {
            "app_id": FABINGO_APP_ID,
            "page_id": FABINGO_PAGE_ID,
            "configured_page_id": settings.META_PAGE_ID,
            "app_mode": "Development (do not switch this app to Live from the bot)",
        },
        "ai": {
            "can_generate_replies": settings.ai_reply_ready,
            "providers": settings.direct_ai_providers,
            "multi_ai_configured": settings.multi_ai_configured,
            "missing_keys": settings.missing_ai_keys,
            "note": (
                "The default MULTI_AI_API_URL of http://localhost:8000 is not treated "
                "as configured. Set MULTI_AI_API_URL only when that service is running."
            ),
        },
        "meta": {
            "app_secret_set": bool(settings.META_APP_SECRET),
            "verify_token_set": bool(settings.META_VERIFY_TOKEN),
            "page_access_token_set": bool(settings.META_PAGE_ACCESS_TOKEN),
            "page_id": settings.META_PAGE_ID or None,
            "whatsapp_token_set": bool(settings.META_WHATSAPP_TOKEN),
            "whatsapp_phone_id_set": bool(settings.META_WHATSAPP_PHONE_ID),
            "instagram_id_set": bool(settings.META_IG_ID),
            "note": "Meta tokens receive and send. They are not AI keys.",
        },
        "channels": channels,
        "flags": {
            "ENABLE_MESSENGER": settings.messenger_enabled,
            "ENABLE_WHATSAPP": settings.whatsapp_enabled,
            "ENABLE_INSTAGRAM": settings.instagram_enabled,
            "ENABLE_PAGE_COMMENTS": settings.page_comments_enabled,
            "ENABLE_PAGE_REACTIONS": settings.page_reactions_enabled,
            "ENABLE_MESSENGER_POLLING": settings.messenger_polling_enabled,
        },
        "store": settings.STORE_NAME,
        "backend_url": settings.FASHION_ARC_API_URL,
        "supabase": settings.supabase_configured,
        "active_conversations": active_conversations,
    }
