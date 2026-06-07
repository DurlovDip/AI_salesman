"""
WhatsApp Cloud API
==================
Async wrapper around the WhatsApp Business Cloud API.
https://developers.facebook.com/docs/whatsapp/cloud-api

All methods are non-blocking and use httpx for async HTTP.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional

import httpx

from config import settings
from messaging.messenger_api import clean_markdown

logger = logging.getLogger(__name__)

GRAPH_API = "https://graph.facebook.com/v21.0"


async def _send(payload: Dict[str, Any]) -> Dict:
    """Send a payload to the WhatsApp Cloud API."""
    url = f"{GRAPH_API}/{settings.META_WHATSAPP_PHONE_ID}/messages"
    headers = {
        "Authorization": f"Bearer {settings.META_WHATSAPP_TOKEN}",
        "Content-Type": "application/json",
    }

    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.post(url, headers=headers, json=payload)

    data = response.json()
    if response.status_code not in (200, 201):
        logger.error(f"WhatsApp API error: {data}")
    else:
        msg_id = (
            data.get("messages", [{}])[0].get("id", "ok")
            if data.get("messages")
            else "ok"
        )
        logger.debug(f"WhatsApp sent: {msg_id}")

    return data


# ── Text Messages ─────────────────────────────────────────────────────────


async def send_text(phone: str, text: str, preview_url: bool = False) -> Dict:
    """
    Send a plain text message.
    phone: recipient phone number in international format (e.g. "8801712345678")
    """
    return await _send({
        "messaging_product": "whatsapp",
        "to": phone,
        "type": "text",
        "text": {"body": text, "preview_url": preview_url},
    })


async def send_text_chunked(phone: str, text: str) -> None:
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
        
    # Send each chunk with a small delay to simulate human typing
    for i, chunk in enumerate(chunks):
        if i > 0:
            await asyncio.sleep(1.5)  # 1.5 seconds typing pause
        await send_text(phone, chunk)


# ── Interactive Buttons ──────────────────────────────────────────────────


async def send_interactive_buttons(
    phone: str,
    body: str,
    buttons: List[Dict[str, str]],
    header: Optional[str] = None,
    footer: Optional[str] = None,
) -> Dict:
    """
    Send an interactive button message (max 3 buttons).

    buttons: [{"id": "btn_1", "title": "View Product"}, ...]
    """
    action_buttons = [
        {"type": "reply", "reply": {"id": b["id"], "title": b["title"][:20]}}
        for b in buttons[:3]
    ]

    interactive: Dict[str, Any] = {
        "type": "button",
        "body": {"text": body},
        "action": {"buttons": action_buttons},
    }

    if header:
        interactive["header"] = {"type": "text", "text": header}
    if footer:
        interactive["footer"] = {"text": footer}

    return await _send({
        "messaging_product": "whatsapp",
        "to": phone,
        "type": "interactive",
        "interactive": interactive,
    })


# ── Interactive List ─────────────────────────────────────────────────────


async def send_interactive_list(
    phone: str,
    body: str,
    button_text: str,
    sections: List[Dict],
    header: Optional[str] = None,
    footer: Optional[str] = None,
) -> Dict:
    """
    Send an interactive list message.

    sections: [{
        "title": "Products",
        "rows": [
            {"id": "row_1", "title": "Product Name", "description": "৳999"},
            ...
        ]
    }]
    """
    interactive: Dict[str, Any] = {
        "type": "list",
        "body": {"text": body},
        "action": {
            "button": button_text[:20],
            "sections": sections,
        },
    }

    if header:
        interactive["header"] = {"type": "text", "text": header}
    if footer:
        interactive["footer"] = {"text": footer}

    return await _send({
        "messaging_product": "whatsapp",
        "to": phone,
        "type": "interactive",
        "interactive": interactive,
    })


# ── Image Message ────────────────────────────────────────────────────────


async def send_image(
    phone: str,
    image_url: str,
    caption: Optional[str] = None,
) -> Dict:
    """Send an image by URL with optional caption."""
    image_payload: Dict[str, Any] = {"link": image_url}
    if caption:
        image_payload["caption"] = caption

    return await _send({
        "messaging_product": "whatsapp",
        "to": phone,
        "type": "image",
        "image": image_payload,
    })


# ── Template Message ─────────────────────────────────────────────────────


async def send_template(
    phone: str,
    template_name: str,
    language_code: str = "en_US",
    components: Optional[List[Dict]] = None,
) -> Dict:
    """
    Send a pre-approved template message.
    Required for starting conversations (outside 24-hour window).
    """
    template: Dict[str, Any] = {
        "name": template_name,
        "language": {"code": language_code},
    }
    if components:
        template["components"] = components

    return await _send({
        "messaging_product": "whatsapp",
        "to": phone,
        "type": "template",
        "template": template,
    })


# ── Reaction ─────────────────────────────────────────────────────────────


async def send_reaction(phone: str, message_id: str, emoji: str) -> Dict:
    """React to a message with an emoji."""
    return await _send({
        "messaging_product": "whatsapp",
        "to": phone,
        "type": "reaction",
        "reaction": {"message_id": message_id, "emoji": emoji},
    })


# ── Read Receipt ─────────────────────────────────────────────────────────


async def mark_read(message_id: str) -> Dict:
    """Mark a message as read (blue checkmarks)."""
    url = f"{GRAPH_API}/{settings.META_WHATSAPP_PHONE_ID}/messages"
    headers = {
        "Authorization": f"Bearer {settings.META_WHATSAPP_TOKEN}",
        "Content-Type": "application/json",
    }

    payload = {
        "messaging_product": "whatsapp",
        "status": "read",
        "message_id": message_id,
    }

    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.post(url, headers=headers, json=payload)

    return response.json()


# ── Media Retrieval ──────────────────────────────────────────────────────


async def get_media_url(media_id: str) -> Optional[str]:
    """Retrieve temporary download URL for WhatsApp media ID."""
    url = f"{GRAPH_API}/{media_id}"
    headers = {
        "Authorization": f"Bearer {settings.META_WHATSAPP_TOKEN}",
    }

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.get(url, headers=headers)
        if response.status_code == 200:
            return response.json().get("url")
        else:
            logger.error(f"WhatsApp Media URL resolution error: {response.text}")
            return None
    except Exception as e:
        logger.error(f"Failed to resolve WhatsApp media ID {media_id}: {e}")
        return None

