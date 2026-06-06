"""
Facebook Messenger Webhook
==========================
Handles incoming Messenger messages and postbacks from Meta.

GET  /webhook/messenger — Verification challenge from Meta
POST /webhook/messenger — Incoming messages, postbacks, deliveries
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
from typing import Any, Dict

from fastapi import APIRouter, HTTPException, Query, Request

from config import settings
from agent.salesman import get_ai_response
from conversation.manager import conversation_manager
from conversation.formatter import (
    format_products_messenger,
    format_product_details,
    format_order_status,
)
from messaging import messenger_api

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/webhook", tags=["Messenger Webhook"])


# ── Webhook Verification ─────────────────────────────────────────────────


@router.get("/messenger")
async def verify_messenger(
    hub_mode: str = Query(None, alias="hub.mode"),
    hub_verify_token: str = Query(None, alias="hub.verify_token"),
    hub_challenge: str = Query(None, alias="hub.challenge"),
):
    """
    Meta webhook verification endpoint.
    Meta sends a GET request with a challenge to verify your server.
    """
    if hub_mode == "subscribe" and hub_verify_token == settings.META_VERIFY_TOKEN:
        logger.info("✅ Messenger webhook verified successfully")
        return int(hub_challenge)

    logger.warning(
        f"❌ Messenger webhook verification failed: mode={hub_mode}, token={hub_verify_token}"
    )
    raise HTTPException(status_code=403, detail="Verification failed")


# ── Incoming Messages ────────────────────────────────────────────────────


@router.post("/messenger")
async def handle_messenger(request: Request):
    """
    Receive and process incoming Messenger messages.
    Meta sends webhook events here for messages, postbacks, etc.
    """
    body = await request.body()

    # Verify signature (security)
    if settings.META_APP_SECRET:
        signature = request.headers.get("X-Hub-Signature-256", "")
        if not _verify_signature(body, signature):
            logger.warning("❌ Invalid Messenger webhook signature")
            raise HTTPException(status_code=403, detail="Invalid signature")

    data = json.loads(body)

    # Meta always sends object: "page"
    if data.get("object") != "page":
        return {"status": "ignored"}

    # Process each entry
    for entry in data.get("entry", []):
        for messaging_event in entry.get("messaging", []):
            await _process_messenger_event(messaging_event)

    # Must return 200 quickly to avoid Meta retries
    return {"status": "ok"}


# ── Event Processing ─────────────────────────────────────────────────────


async def _process_messenger_event(event: Dict[str, Any]) -> None:
    """Route a Messenger event to the appropriate handler."""
    sender_id = event.get("sender", {}).get("id", "")
    if not sender_id:
        return

    # Skip messages from the page itself (echo)
    if sender_id == settings.META_PAGE_ID:
        return

    if "message" in event:
        await _handle_message(sender_id, event["message"])
    elif "postback" in event:
        await _handle_postback(sender_id, event["postback"])
    elif "read" in event:
        logger.debug(f"Message read by {sender_id}")
    elif "delivery" in event:
        logger.debug(f"Message delivered to {sender_id}")


async def _handle_message(sender_id: str, message: Dict) -> None:
    """Handle an incoming text message."""
    # Skip echo messages
    if message.get("is_echo"):
        return

    text = message.get("text", "")
    quick_reply_payload = message.get("quick_reply", {}).get("payload")

    # Handle quick reply selections
    if quick_reply_payload:
        text = quick_reply_payload

    if not text:
        # Handle non-text messages (images, stickers, etc.)
        attachments = message.get("attachments", [])
        if attachments:
            await messenger_api.send_text(
                sender_id,
                "Thanks for sharing! I can help you find products — just describe what you're looking for. 😊"
            )
        return

    # Process the message with AI
    await _respond_to_user(sender_id, text, message_id=message.get("mid"))


async def _handle_postback(sender_id: str, postback: Dict) -> None:
    """Handle a postback (button click)."""
    payload = postback.get("payload", "")
    title = postback.get("title", "")

    logger.info(f"Postback from {sender_id}: {payload}")

    if payload == "GET_STARTED":
        # Welcome message for new users
        profile = await messenger_api.get_user_profile(sender_id)
        name = profile.get("first_name", "there") if profile else "there"

        welcome = (
            f"Hey {name}! 👋 Welcome to {settings.STORE_NAME}!\n\n"
            f"I'm your AI shopping assistant. I can help you:\n"
            f"🔍 Find products\n"
            f"📦 Track orders\n"
            f"❓ Answer questions\n\n"
            f"What are you looking for today?"
        )
        await messenger_api.send_quick_replies(
            sender_id,
            welcome,
            [
                {"title": "🔍 Browse Products", "payload": "Browse your latest products"},
                {"title": "📦 Track Order", "payload": "I want to track my order"},
                {"title": "❓ Help", "payload": "What can you help me with?"},
            ],
        )
        return

    if payload.startswith("PRODUCT_DETAILS_"):
        product_id = payload.replace("PRODUCT_DETAILS_", "")
        text = f"Show me details for product {product_id}"
        await _respond_to_user(sender_id, text)
        return

    # Default: treat postback as text input
    await _respond_to_user(sender_id, title or payload)


# ── AI Response Flow ─────────────────────────────────────────────────────


async def _respond_to_user(sender_id: str, user_text: str, message_id: str | None = None) -> None:
    """
    Process user text through the AI agent and send the response.
    """
    # Fetch profile information from Graph API immediately to register user
    full_name = "Facebook User"
    try:
        profile = await messenger_api.get_user_profile(sender_id)
        if profile:
            first_name = profile.get("first_name", "")
            last_name = profile.get("last_name", "")
            full_name = f"{first_name} {last_name}".strip()
    except Exception as e:
        logger.warning(f"Failed to fetch profile for {sender_id}: {e}")

    # Deduplicate messages using message_id synchronously
    if message_id:
        from database import db
        if db.is_configured():
            is_new = await db.check_and_register_message(
                platform="messenger",
                user_id=sender_id,
                role="user",
                content=user_text,
                message_id=message_id,
                name=full_name
            )
            if not is_new:
                logger.info(f"⏭️ Skipping duplicate/already processed Messenger message {message_id}")
                return

    # Get or create conversation session
    session = await conversation_manager.get_or_create("messenger", sender_id, full_name)
    session.metadata["user_name"] = full_name

    # Track user in Supabase immediately
    from database import db
    if db.is_configured():
        await db.create_or_update_user(
            platform="messenger",
            user_id=sender_id,
            name=full_name
        )
        # Increment message count
        await db.increment_message_count("messenger", sender_id)

    # Add user message to history
    await session.add_message("user", user_text, message_id=message_id)

    # ── TESTER COMMAND INTERCEPTION ───────────────────────────────────────────
    # If the user is a Tester and sends a @testing command, handle it here
    # instead of sending to the AI agent.
    from tester_commands import handle_tester_command
    was_handled, confirmation_reply = await handle_tester_command(
        platform="messenger",
        user_id=sender_id,
        user_text=user_text,
        user_name=full_name,
    )
    if was_handled:
        await session.add_message("assistant", confirmation_reply)
        await messenger_api.send_text_chunked(sender_id, confirmation_reply)
        return
    # ──────────────────────────────────────────────────────────────────────────



    # Show typing indicator
    await messenger_api.mark_seen(sender_id)
    await messenger_api.send_typing_on(sender_id)

    # Check for human handoff
    if session.human_handoff:
        await messenger_api.send_text(
            sender_id,
            "You're currently connected with our support team. "
            "A human agent will respond shortly. "
            "Type 'restart' to start a new conversation with the AI."
        )
        if user_text.lower().strip() == "restart":
            await session.reset()
            await messenger_api.send_text(sender_id, "Fresh start! 🔄 How can I help you?")
        await messenger_api.send_typing_off(sender_id)
        return

    try:
        # Get AI response with tools
        response_text, _ = await get_ai_response(
            messages=session.get_chat_messages(),
            platform="messenger",
            user_id=sender_id,
        )

        # Add assistant response to history
        await session.add_message("assistant", response_text)

        # Check for human handoff request in response
        if "connecting you with a human" in response_text.lower():
            await session.set_human_handoff(True)

        # Send the response (chunked if long)
        await messenger_api.send_typing_off(sender_id)
        await messenger_api.send_text_chunked(sender_id, response_text)

    except Exception as e:
        logger.error(f"Error processing message from {sender_id}: {e}")
        await messenger_api.send_typing_off(sender_id)
        await messenger_api.send_text(
            sender_id,
            f"Oops! Something went wrong. Please try again or visit {settings.STORE_URL} 🙏"
        )


# ── Signature Verification ──────────────────────────────────────────────


def _verify_signature(payload: bytes, signature_header: str) -> bool:
    """Verify the webhook payload signature from Meta."""
    if not signature_header:
        return False

    try:
        # Format: "sha256=<hash>"
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
