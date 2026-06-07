"""
WhatsApp Business Webhook
=========================
Handles incoming WhatsApp messages via the Cloud API.

GET  /webhook/whatsapp — Verification challenge from Meta
POST /webhook/whatsapp — Incoming messages, status updates
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException, Query, Request

from config import settings
from agent.salesman import get_ai_response
from conversation.manager import conversation_manager
from messaging import whatsapp_api

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/webhook", tags=["WhatsApp Webhook"])


# ── Webhook Verification ─────────────────────────────────────────────────


@router.get("/whatsapp")
async def verify_whatsapp(
    hub_mode: str = Query(None, alias="hub.mode"),
    hub_verify_token: str = Query(None, alias="hub.verify_token"),
    hub_challenge: str = Query(None, alias="hub.challenge"),
):
    """
    Meta webhook verification endpoint for WhatsApp.
    """
    if hub_mode == "subscribe" and hub_verify_token == settings.META_VERIFY_TOKEN:
        logger.info("✅ WhatsApp webhook verified successfully")
        return int(hub_challenge)

    logger.warning(
        f"❌ WhatsApp webhook verification failed: mode={hub_mode}, token={hub_verify_token}"
    )
    raise HTTPException(status_code=403, detail="Verification failed")


# ── Incoming Messages ────────────────────────────────────────────────────


@router.post("/whatsapp")
async def handle_whatsapp(request: Request):
    """
    Receive and process incoming WhatsApp messages.
    """
    body = await request.body()

    # Verify signature
    if settings.META_APP_SECRET:
        signature = request.headers.get("X-Hub-Signature-256", "")
        if not _verify_signature(body, signature):
            logger.warning("❌ Invalid WhatsApp webhook signature")
            raise HTTPException(status_code=403, detail="Invalid signature")

    data = json.loads(body)

    # WhatsApp sends object: "whatsapp_business_account"
    if data.get("object") != "whatsapp_business_account":
        return {"status": "ignored"}

    # Process each entry
    for entry in data.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})

            # Process messages
            messages = value.get("messages", [])
            contacts = value.get("contacts", [])

            for msg in messages:
                contact = _find_contact(contacts, msg.get("from", ""))
                await _process_whatsapp_message(msg, contact)

            # Process status updates
            statuses = value.get("statuses", [])
            for status_update in statuses:
                _handle_status_update(status_update)

    return {"status": "ok"}


# ── Message Processing ───────────────────────────────────────────────────


async def _process_whatsapp_message(
    message: Dict[str, Any],
    contact: Optional[Dict] = None,
) -> None:
    """Route a WhatsApp message to the appropriate handler."""
    phone = message.get("from", "")
    msg_id = message.get("id", "")
    msg_type = message.get("type", "")

    if not phone:
        return

    # Mark message as read
    if msg_id:
        try:
            await whatsapp_api.mark_read(msg_id)
        except Exception:
            pass

    text = ""
    image_url = None

    if msg_type == "text":
        text = message.get("text", {}).get("body", "")

    elif msg_type == "interactive":
        interactive = message.get("interactive", {})
        interactive_type = interactive.get("type", "")

        if interactive_type == "button_reply":
            text = interactive.get("button_reply", {}).get("title", "")
        elif interactive_type == "list_reply":
            text = interactive.get("list_reply", {}).get("title", "")
            # Check if it's a product selection
            row_id = interactive.get("list_reply", {}).get("id", "")
            if row_id.startswith("product_"):
                product_id = row_id.replace("product_", "")
                text = f"Show me details for product {product_id}"

    elif msg_type == "image":
        text = message.get("image", {}).get("caption", "")
        media_id = message.get("image", {}).get("id")
        if media_id:
            from messaging import whatsapp_api
            image_url = await whatsapp_api.get_media_url(media_id)

    elif msg_type in ("audio", "video", "document", "sticker"):
        await whatsapp_api.send_text(
            phone,
            "Thanks! I work best with text messages. "
            "Tell me what you're looking for and I'll find it for you! 🔍"
        )
        return

    elif msg_type == "location":
        await whatsapp_api.send_text(
            phone,
            f"Thanks for your location! 📍 We deliver across Bangladesh. "
            f"Visit {settings.STORE_URL} to place an order!"
        )
        return

    if not text and not image_url:
        return

    # Process with AI
    await _respond_to_user(phone, text, contact, message_id=msg_id, image_url=image_url)


# ── AI Response Flow ─────────────────────────────────────────────────────


async def _respond_to_user(
    phone: str,
    user_text: str,
    contact: Optional[Dict] = None,
    message_id: str | None = None,
    image_url: str | None = None,
) -> None:
    """
    Process user text through the AI agent and send the response via WhatsApp.
    """
    # Get user name from contact info
    user_name = None
    if contact:
        profile = contact.get("profile", {})
        user_name = profile.get("name")

    # Format content to save: plain text or multimodal list
    content_to_save = user_text
    if image_url:
        content_to_save = [
            {"type": "text", "text": user_text or "Describe this image and search for matching products in the store catalog."},
            {"type": "image_url", "image_url": {"url": image_url}}
        ]

    # Deduplicate messages using message_id synchronously
    if message_id:
        from database import db
        if db.is_configured():
            is_new = await db.check_and_register_message(
                platform="whatsapp",
                user_id=phone,
                role="user",
                content=content_to_save,
                message_id=message_id,
                name=user_name
            )
            if not is_new:
                logger.info(f"⏭️ Skipping duplicate/already processed WhatsApp message {message_id}")
                return

    # Get or create conversation session
    session = await conversation_manager.get_or_create("whatsapp", phone, user_name)

    # Always fetch latest user role/profile from DB and sync with session metadata
    from database import db
    if db.is_configured():
        user_data = await db.get_user("whatsapp", phone)
        if user_data:
            user_role = user_data.get("role") or user_data.get("metadata", {}).get("role", "Customer")
            session.metadata["role"] = user_role

    # Track user and increment message count in Supabase in background
    if db.is_configured():
        import asyncio
        async def _track_user_background():
            try:
                await db.create_or_update_user(
                    platform="whatsapp",
                    user_id=phone,
                    name=user_name or "WhatsApp User"
                )
                await db.increment_message_count("whatsapp", phone)
            except Exception as e:
                logger.error(f"Error updating WhatsApp user profile in background: {e}")
        asyncio.create_task(_track_user_background())


    # Check for human handoff
    if session.human_handoff:
        await whatsapp_api.send_text(
            phone,
            "You're currently connected with our support team. "
            "A human agent will respond shortly. "
            "Type 'restart' to start a new conversation with the AI."
        )
        if user_text.lower().strip() == "restart":
            await session.reset()
            await whatsapp_api.send_text(phone, "Fresh start! 🔄 How can I help you?")
        return

    # Welcome new users
    if not session.messages:
        greeting = user_name or "there"
        welcome = (
            f"Assalamu Alaikum {greeting}! 👋\n\n"
            f"Welcome to {settings.STORE_NAME}!\n"
            f"I'm your AI shopping assistant.\n\n"
            f"I can help you:\n"
            f"🔍 Find products\n"
            f"📦 Track orders\n"
            f"❓ Answer questions\n\n"
            f"What are you looking for today?"
        )
        await whatsapp_api.send_text(phone, welcome)

    # Add user message to history
    await session.add_message("user", content_to_save, message_id=message_id)

    # ── DOMAIN FILTERING ──────────────────────────────────────────────────────
    # Exclusively respond to roles based on global reply domain (1 = Admin, 2 = Admin/Tester, 3 = All)
    reply_domain = await db.get_reply_domain()
    user_role = session.metadata.get("role", "Customer")
    if reply_domain == "1":
        if user_role != "Admin":
            logger.info(f"🚫 Ignoring WhatsApp message from {phone} because domain is set to Admin only.")
            return
    elif reply_domain == "2":
        if user_role not in ("Admin", "Tester"):
            logger.info(f"🚫 Ignoring WhatsApp message from {phone} because domain is set to Admin+Tester only.")
            return

    # ── TESTER COMMAND INTERCEPTION ───────────────────────────────────────────
    from tester_commands import handle_tester_command
    was_handled, confirmation_reply = await handle_tester_command(
        platform="whatsapp",
        user_id=phone,
        user_text=user_text,
        user_name=user_name or "WhatsApp Tester",
    )
    if was_handled:
        await session.add_message("assistant", confirmation_reply)
        await whatsapp_api.send_text_chunked(phone, confirmation_reply)
        return


    try:
        # Get AI response with tools
        response_text, _ = await get_ai_response(
            messages=session.get_chat_messages(),
            platform="whatsapp",
            user_id=phone,
        )

        # Process AI-initiated commands and clean the output
        from tester_commands import handle_ai_response_commands
        response_text = await handle_ai_response_commands(
            platform="whatsapp",
            user_id=phone,
            text=response_text
        )

        if response_text:
            # Extract any [IMAGE: url] tags
            import re
            img_pattern = r'\[IMAGE:\s*(https?://[^\s\]]+)\]'
            image_urls = re.findall(img_pattern, response_text)
            response_text = re.sub(img_pattern, '', response_text).strip()
            response_text = re.sub(r'[ \t]+', ' ', response_text).strip()

            assistant_content = response_text
            if image_urls:
                assistant_content = [
                    {"type": "text", "text": response_text},
                ] + [
                    {"type": "image_url", "image_url": {"url": img_url}}
                    for img_url in image_urls
                ]

            # Add assistant response to history
            await session.add_message("assistant", assistant_content)

            # Check for human handoff
            if "connecting you with a human" in response_text.lower():
                await session.set_human_handoff(True)

            # Send the response (chunked if long)
            if response_text:
                await whatsapp_api.send_text_chunked(phone, response_text)

            # Send any extracted images
            for img_url in image_urls:
                await whatsapp_api.send_image(phone, img_url)

    except Exception as e:
        logger.error(f"Error processing WhatsApp message from {phone}: {e}")
        await whatsapp_api.send_text(
            phone,
            f"Oops! Something went wrong. Please try again or visit {settings.STORE_URL} 🙏"
        )


# ── Status Updates ───────────────────────────────────────────────────────


def _handle_status_update(status: Dict) -> None:
    """Handle message status updates (sent, delivered, read)."""
    msg_id = status.get("id", "")
    recipient = status.get("recipient_id", "")
    status_type = status.get("status", "")

    logger.debug(f"WhatsApp status: {status_type} for {recipient} (msg: {msg_id})")


# ── Helpers ──────────────────────────────────────────────────────────────


def _find_contact(contacts: list, phone: str) -> Optional[Dict]:
    """Find contact info for a phone number."""
    for contact in contacts:
        if contact.get("wa_id") == phone:
            return contact
    return None


def _verify_signature(payload: bytes, signature_header: str) -> bool:
    """Verify the webhook payload signature from Meta."""
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
