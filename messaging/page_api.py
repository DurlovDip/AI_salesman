"""
Facebook Page engagement Graph calls.

Reply: POST /{comment-id}/comments   (pages_manage_engagement)
Like:  POST /{object-id}/likes        (pages_manage_engagement)

Webhook delivery is preferred. These helpers only run after a feed event.
"""

from __future__ import annotations

import logging
from typing import Any, Dict

import httpx

from config import settings

logger = logging.getLogger(__name__)

GRAPH_API = "https://graph.facebook.com/v21.0"


async def reply_to_comment(comment_id: str, message: str) -> Dict[str, Any]:
    """Publish one public reply on a Page comment."""
    if not settings.META_PAGE_ACCESS_TOKEN:
        logger.error("Cannot reply to comment: META_PAGE_ACCESS_TOKEN is not set")
        return {"error": "missing_page_token"}
    if not comment_id or not message:
        return {"error": "missing_comment_or_message"}

    url = f"{GRAPH_API}/{comment_id}/comments"
    params = {"access_token": settings.META_PAGE_ACCESS_TOKEN}
    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.post(url, params=params, json={"message": message})

    data = response.json()
    if response.status_code != 200:
        logger.error("Page comment reply failed: %s", data)
        return {"error": data}
    return data


async def like_object(object_id: str) -> Dict[str, Any]:
    """Like a comment or post. One like per object; this is not a reaction spam loop."""
    if not settings.META_PAGE_ACCESS_TOKEN:
        logger.error("Cannot like object: META_PAGE_ACCESS_TOKEN is not set")
        return {"error": "missing_page_token"}
    if not object_id:
        return {"error": "missing_object_id"}

    url = f"{GRAPH_API}/{object_id}/likes"
    params = {"access_token": settings.META_PAGE_ACCESS_TOKEN}
    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.post(url, params=params)

    try:
        data = response.json()
    except Exception:
        data = {"raw": response.text}
    if response.status_code != 200:
        logger.error("Page like failed: %s", data)
        return {"error": data}
    return data
