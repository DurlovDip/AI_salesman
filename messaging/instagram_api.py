"""
Instagram Messaging send helpers.

Uses the Page linked to the Instagram professional account:
POST https://graph.facebook.com/v21.0/{PAGE_ID}/messages?access_token=PAGE_TOKEN

https://developers.facebook.com/docs/messenger-platform/instagram/features/send-message

This module is scaffolding. It stays idle unless ENABLE_INSTAGRAM=true.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List

import httpx

from config import settings
from messaging.messenger_api import clean_markdown

logger = logging.getLogger(__name__)

GRAPH_API = "https://graph.facebook.com/v21.0"


def _messages_url() -> str:
    page_id = settings.META_PAGE_ID or "me"
    return f"{GRAPH_API}/{page_id}/messages"


async def _send(payload: Dict[str, Any]) -> Dict[str, Any]:
    if not settings.META_PAGE_ACCESS_TOKEN:
        logger.error("Instagram send skipped: META_PAGE_ACCESS_TOKEN is not set")
        return {"error": "missing_page_token"}

    params = {"access_token": settings.META_PAGE_ACCESS_TOKEN}
    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.post(_messages_url(), params=params, json=payload)

    data = response.json()
    if response.status_code != 200:
        logger.error("Instagram API error: %s", data)
    return data


async def send_text(recipient_id: str, text: str) -> Dict[str, Any]:
    """Send a plain text Instagram DM. recipient_id is the Instagram-scoped id (IGSID)."""
    return await _send({
        "recipient": {"id": recipient_id},
        "message": {"text": text},
    })


async def send_text_chunked(recipient_id: str, text: str) -> None:
    """Send a short DM, split into at most three bubbles."""
    cleaned = clean_markdown(text or "")
    if not cleaned:
        return

    paragraphs = [p.strip() for p in cleaned.split("\n\n") if p.strip()]
    if len(paragraphs) == 1 and len(cleaned) > 250:
        paragraphs = [p.strip() for p in cleaned.split("\n") if p.strip()]

    chunks: List[str] = []
    current = ""
    for paragraph in paragraphs:
        if current and len(current) + len(paragraph) + 2 >= 250:
            chunks.append(current)
            current = paragraph
        elif current:
            current = current + "\n\n" + paragraph
        else:
            current = paragraph
    if current:
        chunks.append(current)
    chunks = chunks[:3] or [cleaned[:1000]]

    for index, chunk in enumerate(chunks):
        if index:
            await asyncio.sleep(1.0)
        await send_text(recipient_id, chunk)


async def send_image(recipient_id: str, image_url: str) -> Dict[str, Any]:
    return await _send({
        "recipient": {"id": recipient_id},
        "message": {
            "attachment": {
                "type": "image",
                "payload": {"url": image_url},
            }
        },
    })
