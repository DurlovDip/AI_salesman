"""
Facebook Messenger Send API
============================
Async wrapper around the Messenger Platform Send API.
https://developers.facebook.com/docs/messenger-platform/send-messages

All methods are non-blocking and use httpx for async HTTP.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

import httpx

from config import settings

logger = logging.getLogger(__name__)

GRAPH_API = "https://graph.facebook.com/v21.0"


async def _send(payload: Dict[str, Any]) -> Dict:
    """Send a payload to the Messenger Send API."""
    url = f"{GRAPH_API}/me/messages"
    params = {"access_token": settings.META_PAGE_ACCESS_TOKEN}

    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.post(url, params=params, json=payload)

    data = response.json()
    if response.status_code != 200:
        logger.error(f"Messenger API error: {data}")
    else:
        logger.debug(f"Messenger sent: {data.get('message_id', 'ok')}")

    return data


# ── Text Messages ─────────────────────────────────────────────────────────


async def send_text(recipient_id: str, text: str) -> Dict:
    """Send a plain text message."""
    return await _send({
        "recipient": {"id": recipient_id},
        "message": {"text": text},
        "messaging_type": "RESPONSE",
    })


import asyncio
import re


def clean_markdown(text: str) -> str:
    """Remove markdown bold/italic signs, lists, headers, links, and other elements for clean chat style."""
    # Replace markdown links: [Text](url) -> Text - url
    text = re.sub(r'\[([^\]]+)\]\(([^)]+)\)', r'\1 - \2', text)
    # Replace list markers like "* " or "- " at start of lines with "• "
    text = re.sub(r'^\s*[\*\-]\s+', '• ', text, flags=re.MULTILINE)
    # Remove headers: #, ##, ### at start of lines
    text = re.sub(r'^\s*#+\s+', '', text, flags=re.MULTILINE)
    # Remove blockquote markers
    text = re.sub(r'^\s*>\s+', '', text, flags=re.MULTILINE)
    # Remove bold/italic/strikethrough markers (**text**, __text__, *text*, _text_, ~~text~~)
    text = re.sub(r'\*\*|__', '', text)
    text = re.sub(r'\*|_|~~', '', text)
    # Remove inline code backticks `text`
    text = re.sub(r'`', '', text)
    return text.strip()



async def send_text_chunked(recipient_id: str, text: str, chunk_size: int = 2000) -> None:
    """
    Send a text message, cleaning markdown and splitting it into natural human-like chunks
    (e.g., splitting by paragraphs or newlines) to avoid sending one massive message.
    """
    cleaned_text = clean_markdown(text)
    if not cleaned_text:
        return
        
    # Split into natural paragraphs/chunks
    paragraphs = [p.strip() for p in cleaned_text.split('\n\n') if p.strip()]
    
    # If there are no double newlines but the text is long, split by single newlines
    if len(paragraphs) == 1 and len(cleaned_text) > 250:
        paragraphs = [p.strip() for p in cleaned_text.split('\n') if p.strip()]
        
    # Re-group paragraphs if some are very short (to avoid sending too many tiny messages)
    chunks = []
    current_chunk = ""
    for p in paragraphs:
        if len(current_chunk) + len(p) + 2 < 250:  # target max chunk length for human message
            if current_chunk:
                current_chunk += "\n\n" + p
            else:
                current_chunk = p
        else:
            if current_chunk:
                chunks.append(current_chunk)
            current_chunk = p
    if current_chunk:
        chunks.append(current_chunk)
        
    if not chunks:
        chunks = [cleaned_text]
        
    # Limit to at most 3 messages at once by merging adjacent chunks
    if len(chunks) > 3:
        while len(chunks) > 3:
            min_idx = 0
            min_len = len(chunks[0]) + len(chunks[1])
            for idx in range(1, len(chunks) - 1):
                combined_len = len(chunks[idx]) + len(chunks[idx+1])
                if combined_len < min_len:
                    min_len = combined_len
                    min_idx = idx
            chunks[min_idx] = chunks[min_idx] + "\n\n" + chunks[min_idx+1]
            chunks.pop(min_idx+1)
        
    # Send each chunk with a small delay and typing indicator to simulate human typing
    for i, chunk in enumerate(chunks):
        if i > 0:
            await send_typing_on(recipient_id)
            await asyncio.sleep(1.5)  # 1.5 seconds typing pause
            await send_typing_off(recipient_id)
        await send_text(recipient_id, chunk)


# ── Quick Replies ─────────────────────────────────────────────────────────


async def send_quick_replies(
    recipient_id: str,
    text: str,
    replies: List[Dict[str, str]],
) -> Dict:
    """
    Send a text message with quick reply buttons.

    replies: [{"title": "Yes", "payload": "YES"}, ...]
    """
    quick_replies = [
        {
            "content_type": "text",
            "title": r["title"][:20],  # Max 20 chars
            "payload": r.get("payload", r["title"].upper()),
        }
        for r in replies[:13]  # Max 13 quick replies
    ]

    return await _send({
        "recipient": {"id": recipient_id},
        "message": {"text": text, "quick_replies": quick_replies},
        "messaging_type": "RESPONSE",
    })


# ── Generic Template (Product Carousel) ──────────────────────────────────


async def send_generic_template(
    recipient_id: str,
    elements: List[Dict],
) -> Dict:
    """
    Send a carousel of cards (Generic Template).
    Each element: {title, subtitle, image_url, buttons, default_action}
    Max 10 elements.
    """
    return await _send({
        "recipient": {"id": recipient_id},
        "message": {
            "attachment": {
                "type": "template",
                "payload": {
                    "template_type": "generic",
                    "elements": elements[:10],
                },
            }
        },
        "messaging_type": "RESPONSE",
    })


# ── Button Template ──────────────────────────────────────────────────────


async def send_button_template(
    recipient_id: str,
    text: str,
    buttons: List[Dict],
) -> Dict:
    """
    Send a text message with buttons below it.
    Max 3 buttons.
    """
    return await _send({
        "recipient": {"id": recipient_id},
        "message": {
            "attachment": {
                "type": "template",
                "payload": {
                    "template_type": "button",
                    "text": text,
                    "buttons": buttons[:3],
                },
            }
        },
        "messaging_type": "RESPONSE",
    })


# ── Image Message ────────────────────────────────────────────────────────


async def send_image(recipient_id: str, image_url: str) -> Dict:
    """Send an image by URL."""
    return await _send({
        "recipient": {"id": recipient_id},
        "message": {
            "attachment": {
                "type": "image",
                "payload": {"url": image_url, "is_reusable": True},
            }
        },
        "messaging_type": "RESPONSE",
    })


# ── Sender Actions ───────────────────────────────────────────────────────


async def send_typing_on(recipient_id: str) -> Dict:
    """Show typing indicator."""
    return await _send({
        "recipient": {"id": recipient_id},
        "sender_action": "typing_on",
    })


async def send_typing_off(recipient_id: str) -> Dict:
    """Hide typing indicator."""
    return await _send({
        "recipient": {"id": recipient_id},
        "sender_action": "typing_off",
    })


async def mark_seen(recipient_id: str) -> Dict:
    """Mark messages as seen (blue checkmark)."""
    return await _send({
        "recipient": {"id": recipient_id},
        "sender_action": "mark_seen",
    })


# ── User Profile ─────────────────────────────────────────────────────────


async def get_user_profile(user_id: str) -> Optional[Dict]:
    """
    Fetch user's public profile (first_name, last_name, profile_pic).
    Requires pages_messaging permission.
    """
    url = f"{GRAPH_API}/{user_id}"
    params = {
        "fields": "first_name,last_name,profile_pic",
        "access_token": settings.META_PAGE_ACCESS_TOKEN,
    }

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(url, params=params)
        if response.status_code == 200:
            return response.json()
    except Exception as e:
        logger.warning(f"Failed to fetch user profile for {user_id}: {e}")

    return None
