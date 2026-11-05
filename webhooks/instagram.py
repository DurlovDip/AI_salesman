"""
Instagram Messaging webhook
===========================
GET  /webhook/instagram — Meta verification challenge
POST /webhook/instagram — Instagram DM events (object: "instagram")

Idle unless ENABLE_INSTAGRAM=true. Verification still answers so the
product can be subscribed before the flag is turned on.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict

from fastapi import APIRouter, HTTPException, Query, Request, Response

from config import settings
from webhooks.signature import verify_meta_signature

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/webhook", tags=["Instagram Webhook"])

_IMAGE_TAG = re.compile(r"\[IMAGE:\s*(https?://[^\s\]]+)\]")


@router.get("/instagram")
async def verify_instagram(
    hub_mode: str = Query(None, alias="hub.mode"),
    hub_verify_token: str = Query(None, alias="hub.verify_token"),
    hub_challenge: str = Query(None, alias="hub.challenge"),
):
    if hub_mode == "subscribe" and hub_verify_token == settings.META_VERIFY_TOKEN:
        logger.info("Instagram webhook verified")
        return Response(content=hub_challenge or "", media_type="text/plain")

    logger.warning("Instagram webhook verification failed (mode=%s)", hub_mode)
    raise HTTPException(status_code=403, detail="Verification failed")


@router.post("/instagram")
async def handle_instagram(request: Request):
    body = await request.body()
    if settings.META_APP_SECRET:
        signature = request.headers.get("X-Hub-Signature-256", "")
        if not verify_meta_signature(body, signature):
            logger.warning("Invalid Instagram webhook signature")
            raise HTTPException(status_code=403, detail="Invalid signature")

    data = json.loads(body)
    if data.get("object") != "instagram":
        return {"status": "ignored", "reason": "expected object instagram"}

    if not settings.instagram_enabled:
        logger.info("Instagram webhook received but ENABLE_INSTAGRAM is false")
        return {"status": "disabled", "channel": "instagram"}

    if not settings.instagram_configured:
        logger.error("Instagram enabled but META_PAGE_ACCESS_TOKEN is missing")
        return {
            "status": "not_configured",
            "channel": "instagram",
            "missing": ["META_PAGE_ACCESS_TOKEN"],
        }

    for entry in data.get("entry", []):
        for event in entry.get("messaging", []):
            await _process_event(event)

    return {"status": "ok"}


async def _process_event(event: Dict[str, Any]) -> None:
    sender_id = str((event.get("sender") or {}).get("id") or "")
    if not sender_id:
        return
    own_ids = {str(settings.META_PAGE_ID or ""), str(settings.META_IG_ID or "")}
    if sender_id in own_ids:
        return

    message = event.get("message") or {}
    if message.get("is_echo"):
        return

    if message:
        text = message.get("text") or ""
        if message.get("quick_reply", {}).get("payload"):
            text = message["quick_reply"]["payload"]
        image_url = None
        for attachment in message.get("attachments") or []:
            if attachment.get("type") == "image":
                image_url = (attachment.get("payload") or {}).get("url")
                break
        if text or image_url:
            await _respond(sender_id, text, message.get("mid"), image_url)
        return

    postback = event.get("postback") or {}
    if postback:
        await _respond(sender_id, postback.get("title") or postback.get("payload") or "", None, None)


async def _respond(
    sender_id: str,
    user_text: str,
    message_id: str | None,
    image_url: str | None,
) -> None:
    from agent.salesman import get_ai_response
    from conversation.manager import conversation_manager
    from messaging import instagram_api

    if not settings.ai_reply_ready:
        logger.warning(
            "Instagram DM from %s not answered: no AI key. Page token state is separate.",
            sender_id,
        )
        await instagram_api.send_text(
            sender_id,
            f"Our shopping assistant isn't available right now. Please visit {settings.STORE_URL}",
        )
        return

    content: Any = user_text
    if image_url:
        content = [
            {"type": "text", "text": user_text or ""},
            {"type": "image_url", "image_url": {"url": image_url}},
        ]

    session = await conversation_manager.get_or_create("instagram", sender_id)
    await session.add_message("user", content, message_id=message_id)

    try:
        response_text, _ = await get_ai_response(
            messages=session.get_chat_messages(),
            platform="instagram",
            user_id=sender_id,
        )
    except Exception:
        logger.exception("Instagram AI call failed for %s", sender_id)
        await instagram_api.send_text(
            sender_id,
            f"Sorry, something went wrong. Please try again or visit {settings.STORE_URL}",
        )
        return

    if not response_text:
        return

    image_urls = _IMAGE_TAG.findall(response_text)
    response_text = _IMAGE_TAG.sub("", response_text).strip()
    await session.add_message("assistant", response_text)
    if response_text:
        await instagram_api.send_text_chunked(sender_id, response_text)
    for url in image_urls:
        await instagram_api.send_image(sender_id, url)
