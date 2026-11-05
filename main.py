"""
AI Salesman — Main Entry Point
================================
Facebook Messenger & WhatsApp AI Sales Assistant for Fashion ARC.

Receives customer messages via Meta webhooks, processes them through
an AI agent with tool calling, and responds with product recommendations,
order tracking, and store information.

Run:
    uvicorn main:app --host 0.0.0.0 --port 8001 --reload
"""

from __future__ import annotations

import asyncio
import httpx
import logging
import os
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Body, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, HTMLResponse

from config import settings
from conversation.manager import conversation_manager
from messaging import messenger_api


# ── Logging Setup ────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.DEBUG if settings.DEBUG else logging.INFO,
    format="%(asctime)s │ %(name)-28s │ %(levelname)-5s │ %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("ai_salesman")


# ── Lifecycle ────────────────────────────────────────────────────────────

async def _cleanup_sessions_loop() -> None:
    """Periodically clean up expired conversation sessions."""
    while True:
        await asyncio.sleep(300)  # Every 5 minutes
        removed = conversation_manager.cleanup_expired()
        if removed > 0:
            logger.info(f"🧹 Cleaned up {removed} expired conversation(s)")


async def _poll_facebook_loop() -> None:
    """
    Periodically poll Facebook for new messages from Dip Durlov.
    This acts as a webhook fallback for local testing without ngrok.
    """
    logger.info("🔁 Started Facebook message polling loop (local webhook emulator)")
    while True:
        await asyncio.sleep(5)  # Poll every 5 seconds
        if not settings.messenger_enabled or not settings.messenger_configured:
            continue
        try:
            page_id = settings.META_PAGE_ID
            access_token = settings.META_PAGE_ACCESS_TOKEN
            url = f"https://graph.facebook.com/v21.0/{page_id}/conversations"
            params = {
                "fields": "id,participants,updated_time,unread_count",
                "access_token": access_token
            }
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.get(url, params=params)

            if response.status_code != 200:
                continue

            data = response.json()
            for item in data.get("data", []):
                part_data = item.get("participants", {}).get("data", [])
                p_id = "unknown"
                p_name = "Facebook User"
                if part_data:
                    other_parts = [p for p in part_data if p.get("id") != page_id]
                    p_info = other_parts[0] if other_parts else part_data[0]
                    p_id = p_info.get("id", "unknown")
                    p_name = p_info.get("name", "Facebook User")

                # Track user in Supabase immediately
                if p_id != "unknown":
                    from database import db
                    if db.is_configured():
                        await db.create_or_update_user(
                            platform="messenger",
                            user_id=p_id,
                            name=p_name
                        )

                session = await conversation_manager.get_or_create("messenger", p_id)

                # Always fetch latest user role/profile from DB and sync with session metadata
                if db.is_configured():
                    user_data = await db.get_user("messenger", p_id)
                    if user_data:
                        user_role = user_data.get("role") or user_data.get("metadata", {}).get("role", "Customer")
                        session.metadata["role"] = user_role

                # Exclusively respond to roles based on global reply domain (1 = Admin, 2 = Admin/Tester, 3 = All)
                reply_domain = await db.get_reply_domain()
                user_role = session.metadata.get("role", "Customer")
                if reply_domain == "1":
                    if user_role != "Admin":
                        continue
                elif reply_domain == "2":
                    if user_role not in ("Admin", "Tester"):
                        continue
                if session.is_processing:
                    continue

                # Fetch message history to see if the last message is from the user
                conv_id = item.get("id")
                msg_url = f"https://graph.facebook.com/v25.0/{conv_id}"
                msg_params = {
                    "fields": "messages.limit(10){message,from,created_time,attachments}",
                    "access_token": access_token
                }
                async with httpx.AsyncClient(timeout=10.0) as client2:
                    msg_response = await client2.get(msg_url, params=msg_params)

                if msg_response.status_code != 200:
                    continue

                msg_data = msg_response.json()
                fb_messages = msg_data.get("messages", {}).get("data", [])
                if not fb_messages:
                    continue

                # Reverse to get chronological order
                fb_messages.reverse()

                # Check if the last message is from the customer
                last_fb_msg = fb_messages[-1]
                last_fb_from_id = last_fb_msg.get("from", {}).get("id")
                last_fb_msg_id = last_fb_msg.get("id")

                if last_fb_from_id != page_id:
                    # Skip if we already responded to this exact message ID
                    if session.metadata.get("last_responded_msg_id") == last_fb_msg_id:
                        continue

                    # Sync local session first to check if we already responded
                    # (In case local session is empty but we have history)
                    session.messages.clear()
                    for msg in fb_messages:
                        f_id = msg.get("from", {}).get("id")
                        m_text = msg.get("message", "")
                        m_id = msg.get("id")
                        
                        # Parse attachments if any
                        attachments = msg.get("attachments", {}).get("data", [])
                        image_url = None
                        for attach in attachments:
                            mime_type = attach.get("mime_type", "")
                            if "image" in mime_type or attach.get("image_data"):
                                image_url = attach.get("image_data", {}).get("url")
                                if not image_url:
                                    image_url = attach.get("payload", {}).get("url")
                                break
                                
                        if not m_text and not image_url:
                            continue
                            
                        # Format content
                        if image_url:
                            m_content = [
                                {"type": "text", "text": m_text or ""},
                                {"type": "image_url", "image_url": {"url": image_url}}
                            ]
                        else:
                            m_content = m_text
                            
                        if f_id == page_id:
                            await session.add_message("assistant", m_content, message_id=m_id)
                        else:
                            await session.add_message("user", m_content, message_id=m_id)

                    # Verify if the last message is still a 'user' message after sync
                    if session.messages and session.messages[-1]["role"] == "user":
                        last_msg_content = session.messages[-1]["content"]
                        user_text = last_msg_content
                        if isinstance(last_msg_content, list):
                            # Extract text block for tester command fuzzy logic if needed
                            user_text = ""
                            for block in last_msg_content:
                                if block.get("type") == "text":
                                    user_text = block.get("text", "")
                                    break
                        logger.info(f"🔄 Polling detected new message from Dip Durlov: '{user_text}'. Triggering AI...")

                        session.is_processing = True
                        try:
                            # ── TESTER COMMAND INTERCEPTION ──────────────────────
                            from tester_commands import handle_tester_command
                            was_handled, confirmation_reply = await handle_tester_command(
                                platform="messenger",
                                user_id=p_id,
                                user_text=user_text,
                                user_name=p_name,
                            )
                            if was_handled:
                                await messenger_api.send_text_chunked(p_id, confirmation_reply)
                                session.metadata["last_responded_msg_id"] = last_fb_msg_id
                                await session.add_message("assistant", confirmation_reply)
                                logger.info(f"🧪 Polling: tester command handled for {p_name}")
                                continue
                            # ────────────────────────────────────────────────────

                            # Show typing indicator
                            await messenger_api.mark_seen(p_id)
                            await messenger_api.send_typing_on(p_id)

                            if not settings.ai_reply_ready:
                                logger.warning(
                                    "Polling saw a Messenger message but no AI key is configured. "
                                    "Not calling the model. META_PAGE_ACCESS_TOKEN is separate."
                                )
                                continue

                            from agent.salesman import get_ai_response
                            response_text, _ = await get_ai_response(
                                messages=session.get_chat_messages(),
                                platform="messenger",
                                user_id=p_id,
                            )

                            if response_text:
                                # Extract any [IMAGE: url] tags
                                import re
                                img_pattern = r'\[IMAGE:\s*(https?://[^\s\]]+)\]'
                                image_urls = re.findall(img_pattern, response_text)
                                cleaned_response_text = re.sub(img_pattern, '', response_text).strip()
                                cleaned_response_text = re.sub(r'[ \t]+', ' ', cleaned_response_text).strip()

                                assistant_content = cleaned_response_text
                                if image_urls:
                                    assistant_content = [
                                        {"type": "text", "text": cleaned_response_text},
                                    ] + [
                                        {"type": "image_url", "image_url": {"url": img_url}}
                                        for img_url in image_urls
                                    ]

                                # Send response to Facebook
                                await messenger_api.send_text_chunked(p_id, cleaned_response_text)
                                for img_url in image_urls:
                                    await messenger_api.send_image(p_id, img_url)

                                # Mark this message ID as responded
                                session.metadata["last_responded_msg_id"] = last_fb_msg_id

                                # Add to session
                                await session.add_message("assistant", assistant_content)
                                logger.info(f"✅ Polling sent AI reply to Dip Durlov: '{cleaned_response_text}'")
                        except Exception as poll_err:
                            logger.error(f"Error generating or sending polling response: {poll_err}")
                        finally:
                            session.is_processing = False
                            await messenger_api.send_typing_off(p_id)
        except Exception as e:
            logger.error(f"Error in polling loop: {e}")


async def sync_local_data_to_supabase() -> None:
    """Migrate/Sync startup data to Supabase."""
    from database import db
    if not db.is_configured():
        return

    # Fetch Fabingo Page conversations and sync user IDs/participants to Supabase
    if settings.messenger_configured:
        import httpx
        try:
            logger.info("👥 Syncing Facebook conversation participants to Supabase on startup...")
            page_id = settings.META_PAGE_ID
            access_token = settings.META_PAGE_ACCESS_TOKEN
            url = f"https://graph.facebook.com/v21.0/{page_id}/conversations"
            params = {
                "fields": "id,participants",
                "access_token": access_token
            }
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.get(url, params=params)

            if response.status_code == 200:
                data = response.json()
                synced_count = 0
                for item in data.get("data", []):
                    part_data = item.get("participants", {}).get("data", [])
                    if part_data:
                        other_parts = [p for p in part_data if p.get("id") != page_id]
                        p_info = other_parts[0] if other_parts else part_data[0]
                        p_name = p_info.get("name", "Facebook User")
                        p_id = p_info.get("id")

                        if p_id and p_id != "unknown":
                            await db.create_or_update_user(
                                platform="messenger",
                                user_id=p_id,
                                name=p_name
                            )
                            synced_count += 1
                logger.info(f"✅ Synced {synced_count} Facebook users to Supabase on startup")
            else:
                logger.error(f"Failed to fetch conversations for startup sync: {response.text}")
        except Exception as e:
            logger.error(f"Error syncing Facebook users to Supabase on startup: {e}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application startup and shutdown."""
    logger.info("🚀 AI Salesman starting up...")

    # Log configuration status
    if settings.messenger_enabled and settings.messenger_configured:
        logger.info("✅ Messenger: enabled and token configured")
    elif settings.messenger_enabled:
        logger.warning("⚠️  Messenger: enabled but META_PAGE_ACCESS_TOKEN is missing")
    else:
        logger.info("ℹ️  Messenger: disabled (ENABLE_MESSENGER=false)")

    if settings.whatsapp_enabled and settings.whatsapp_configured:
        logger.info("✅ WhatsApp: enabled and token configured")
    elif settings.whatsapp_enabled:
        logger.warning("⚠️  WhatsApp: enabled but META_WHATSAPP_TOKEN or META_WHATSAPP_PHONE_ID is missing")
    else:
        logger.info("ℹ️  WhatsApp: disabled (ENABLE_WHATSAPP=false)")

    if settings.instagram_enabled and settings.instagram_configured:
        logger.info("✅ Instagram: enabled and Page token configured")
    elif settings.instagram_enabled:
        logger.warning("⚠️  Instagram: enabled but META_PAGE_ACCESS_TOKEN is missing")
    else:
        logger.info("ℹ️  Instagram: disabled (ENABLE_INSTAGRAM=false)")

    if settings.page_comments_enabled or settings.page_reactions_enabled:
        logger.info(
            "ℹ️  Page engagement: comments=%s reactions=%s",
            settings.page_comments_enabled,
            settings.page_reactions_enabled,
        )
    else:
        logger.info("ℹ️  Page comments and reactions: disabled")

    if settings.ai_reply_ready:
        logger.info("✅ AI replies: at least one provider key is configured")
    else:
        logger.warning(
            "⚠️  AI replies: NOT configured. Set GEMINI_API_KEY, OPENAI_API_KEY, or GROQ_API_KEY. "
            "A Meta Page token does not generate replies."
        )

    logger.info(f"🏪 Store: {settings.STORE_NAME} ({settings.STORE_URL})")
    logger.info(f"🤖 AI API: {settings.MULTI_AI_API_URL}")
    logger.info(f"🛍️ Backend: {settings.FASHION_ARC_API_URL}")

    # Start cleanup task
    cleanup_task = asyncio.create_task(_cleanup_sessions_loop())
    # Start polling task (acts as local webhook fallback)
    # Disable polling loop in production (when DEBUG is False) to rely entirely on webhooks
    polling_task = None
    if settings.messenger_polling_enabled and settings.messenger_enabled:
        polling_task = asyncio.create_task(_poll_facebook_loop())
        logger.info("ℹ️ Messenger polling is ON (ENABLE_MESSENGER_POLLING). Prefer webhooks.")
    else:
        logger.info("ℹ️ Messenger polling is off. Page, Messenger, Instagram, and WhatsApp use webhooks.")
    # Start local data migration task
    await sync_local_data_to_supabase()

    yield

    cleanup_task.cancel()
    if polling_task:
        polling_task.cancel()
    logger.info("👋 AI Salesman shutting down")


# ── FastAPI App ──────────────────────────────────────────────────────────

app = FastAPI(
    title="AI Salesman",
    description=(
        "Facebook Messenger & WhatsApp AI Sales Assistant for Fashion ARC. "
        "Receives customer messages via Meta webhooks, processes them through "
        "an AI agent with tool calling, and responds with product recommendations."
    ),
    version="1.0.0",
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc",
)

# CORS (needed if you add a dashboard later)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Include Webhook Routers ──────────────────────────────────────────────

from webhooks.messenger import router as messenger_router
from webhooks.whatsapp import router as whatsapp_router
from webhooks.instagram import router as instagram_router
from webhooks.page import router as page_router

app.include_router(messenger_router)
app.include_router(whatsapp_router)
app.include_router(instagram_router)
app.include_router(page_router)


# ── Health & Status Endpoints ────────────────────────────────────────────


@app.get("/", tags=["Health"])
async def root():
    """Root endpoint — health check."""
    return {
        "service": "AI Salesman",
        "status": "running",
        "store": settings.STORE_NAME,
        "version": "1.0.0",
    }


@app.get("/health", tags=["Health"])
async def health():
    """Detailed health check. AI keys and Meta tokens are reported separately."""
    from status import health_snapshot

    return health_snapshot(active_conversations=conversation_manager.active_count)


@app.get("/test-webhook", tags=["Health"])
async def test_webhook_config(request: Request):
    """
    Test endpoint to verify webhook configuration.
    Visit this after deployment to get webhook setup instructions.
    """
    base_url = str(request.base_url).rstrip("/")

    return {
        "status": "webhook_test_endpoint_ready",
        "page_id": settings.META_PAGE_ID,
        "verify_token_configured": bool(settings.META_VERIFY_TOKEN),
        "verify_token_hint": "Use the META_VERIFY_TOKEN env value. It is not echoed here.",
        "webhooks": {
            "messenger": f"{base_url}/webhook/messenger",
            "whatsapp": f"{base_url}/webhook/whatsapp",
            "instagram": f"{base_url}/webhook/instagram",
            "page_feed": f"{base_url}/webhook/page",
        },
        "subscribe": {
            "messenger": ["messages", "messaging_postbacks", "message_deliveries", "message_reads"],
            "whatsapp": ["messages"],
            "instagram": ["messages", "messaging_postbacks", "messaging_seen"],
            "page_feed": ["feed"],
        },
        "flags": {
            "ENABLE_MESSENGER": settings.messenger_enabled,
            "ENABLE_WHATSAPP": settings.whatsapp_enabled,
            "ENABLE_INSTAGRAM": settings.instagram_enabled,
            "ENABLE_PAGE_COMMENTS": settings.page_comments_enabled,
            "ENABLE_PAGE_REACTIONS": settings.page_reactions_enabled,
        },
        "troubleshooting": {
            "if_webhook_verification_fails": "META_VERIFY_TOKEN on the server must match the token typed in the Meta console.",
            "if_messages_not_arriving": "Subscribe the Fabingo Page to the product webhook, and check the channel flag.",
            "if_ai_not_responding": "Open /health. ai.can_generate_replies is about AI keys. meta.page_access_token_set is about the Page token. They fail independently.",
        },
    }


@app.get("/api/ai/status", tags=["Health"])
async def get_ai_status():
    """
    Check the status and reachability of all 3 direct AI services:
    Groq, OpenAI, and Gemini.
    """
    status = {
        "groq": {"configured": bool(settings.GROQ_API_KEY), "reachable": False, "error": None},
        "openai": {"configured": bool(settings.OPENAI_API_KEY), "reachable": False, "error": None},
        "gemini": {"configured": bool(settings.GEMINI_API_KEY), "reachable": False, "error": None},
    }

    async def check_groq():
        if not settings.GROQ_API_KEY:
            return
        try:
            headers = {"Authorization": f"Bearer {settings.GROQ_API_KEY}"}
            async with httpx.AsyncClient(timeout=3.0) as client:
                response = await client.get("https://api.groq.com/openai/v1/models", headers=headers)
            if response.status_code == 200:
                status["groq"]["reachable"] = True
            else:
                status["groq"]["error"] = f"HTTP {response.status_code}"
        except Exception as e:
            status["groq"]["error"] = str(e)

    async def check_openai():
        if not settings.OPENAI_API_KEY:
            return
        try:
            headers = {"Authorization": f"Bearer {settings.OPENAI_API_KEY}"}
            async with httpx.AsyncClient(timeout=3.0) as client:
                response = await client.get("https://api.openai.com/v1/models", headers=headers)
            if response.status_code == 200:
                status["openai"]["reachable"] = True
            else:
                status["openai"]["error"] = f"HTTP {response.status_code}"
        except Exception as e:
            status["openai"]["error"] = str(e)

    async def check_gemini():
        if not settings.GEMINI_API_KEY:
            return
        try:
            url = "https://generativelanguage.googleapis.com/v1beta/models"
            params = {"key": settings.GEMINI_API_KEY}
            async with httpx.AsyncClient(timeout=3.0) as client:
                response = await client.get(url, params=params)
            if response.status_code == 200:
                status["gemini"]["reachable"] = True
            else:
                status["gemini"]["error"] = f"HTTP {response.status_code}"
        except Exception as e:
            status["gemini"]["error"] = str(e)

    await asyncio.gather(check_groq(), check_openai(), check_gemini(), return_exceptions=True)
    return status


@app.get("/api/ai/guidelines", tags=["AI Settings"])
async def get_ai_guidelines():
    """Read custom guidelines compiled dynamically from database contexts."""
    try:
        compiled_text = await compile_and_save_active_guidelines()
        return {"guidelines": compiled_text}
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": f"Failed to compile guidelines: {e}"})


@app.post("/api/ai/guidelines", tags=["AI Settings"])
async def save_ai_guidelines(body: dict = Body(...)):
    """Save custom guidelines to the default Standard Sales Rules context in Supabase."""
    guidelines = body.get("guidelines", "")
    from database import db
    if db.is_configured():
        try:
            await db.save_global_context(
                name="Standard Sales Rules",
                text=guidelines,
                description="Default Fabingo AI sales rules and guidelines",
                context_id="c_default",
                context_type="universal",
                is_active=True
            )
            return {"status": "ok", "message": "Guidelines updated in database successfully"}
        except Exception as e:
            return JSONResponse(status_code=500, content={"error": f"Failed to save guidelines to database: {e}"})
    return JSONResponse(status_code=503, content={"error": "Database not configured"})


async def compile_and_save_active_guidelines() -> str:
    """Compile all active contexts (universal + active special) from Supabase."""
    from database import db
    contexts = await db.get_global_contexts()
    compiled_parts = []

    # Sort contexts: universal first, then special so they append in a consistent, logical order
    sorted_contexts = sorted(
        contexts,
        key=lambda c: 0 if c.get("context_type") == "universal" else 1
    )

    for c in sorted_contexts:
        c_type = c.get("context_type", "special")
        is_act = c.get("is_active", False)

        # Universal context is always active, special is active only if checked
        if c_type == "universal" or is_act:
            header = f"# CONTEXT: {c.get('context_name')} ({c_type.upper()})"
            text_body = c.get("text", "").strip()
            if text_body:
                compiled_parts.append(f"{header}\n\n{text_body}")

    compiled_text = "\n\n" + "\n\n# ======================================================\n\n".join(compiled_parts) + "\n"
    logger.info(f"✅ Compiled guidelines from database successfully with {len(compiled_parts)} active contexts")
    return compiled_text


@app.get("/api/contexts", tags=["AI Settings"])
async def get_contexts():
    """Get all saved guidelines contexts from database with default seed support."""
    from database import db
    contexts = await db.get_global_contexts()

    if not contexts:
        default_text = (
            "Always reply in Bangla.\n"
            "Be extremely polite, friendly, and helpful.\n"
            "Keep responses concise and conversational."
        )
        default_ctx = {
            "id": "c_default",
            "context_name": "Standard Sales Rules",
            "description": "Default Fabingo AI sales rules and guidelines",
            "text": default_text,
            "context_type": "universal",
            "is_active": True
        }
        await db.save_global_context(
            name=default_ctx["context_name"],
            text=default_ctx["text"],
            description=default_ctx["description"],
            context_id=default_ctx["id"],
            context_type=default_ctx["context_type"],
            is_active=default_ctx["is_active"]
        )
        contexts = [default_ctx]

    standardized = []
    for c in contexts:
        standardized.append({
            "id": c.get("id"),
            "context_name": c.get("context_name"),
            "description": c.get("description") or "",
            "text": c.get("text"),
            "context_type": c.get("context_type", "special"),
            "is_active": c.get("is_active", False)
        })
    return standardized


@app.post("/api/contexts", tags=["AI Settings"])
async def create_or_update_context(body: dict = Body(...)):
    """Create or update a guidelines context."""
    from database import db
    name = body.get("context_name")
    text = body.get("text")
    description = body.get("description")
    context_id = body.get("id")
    context_type = body.get("context_type", "special")
    is_active = body.get("is_active", False)

    if not name or not text:
        return JSONResponse(status_code=400, content={"error": "Name and Text are required."})

    res = await db.save_global_context(
        name=name,
        text=text,
        description=description,
        context_id=context_id,
        context_type=context_type,
        is_active=is_active
    )

    # Re-compile guidelines.txt based on active contexts
    await compile_and_save_active_guidelines()

    return {"status": "ok", "context": res}


@app.post("/api/contexts/toggle-active", tags=["AI Settings"])
async def toggle_context_active(body: dict = Body(...)):
    """Toggle the active status of a special context."""
    from database import db
    context_id = body.get("id")
    is_active = body.get("is_active", False)

    if not context_id:
        return JSONResponse(status_code=400, content={"error": "Context ID is required."})

    contexts = await db.get_global_contexts()
    target = None
    for c in contexts:
        if c.get("id") == context_id:
            target = c
            break

    if not target:
        return JSONResponse(status_code=404, content={"error": f"Context '{context_id}' not found."})

    if target.get("context_type") == "universal":
        return JSONResponse(status_code=400, content={"error": "Universal contexts are always active and cannot be deactivated."})

    # Update active state in DB
    await db.save_global_context(
        name=target.get("context_name"),
        text=target.get("text"),
        description=target.get("description"),
        context_id=target.get("id"),
        context_type=target.get("context_type"),
        is_active=is_active
    )

    # Re-compile guidelines.txt
    await compile_and_save_active_guidelines()

    return {"status": "ok", "message": f"Context active state set to {is_active}"}


@app.get("/api/contexts/active", tags=["AI Settings"])
async def get_active_context():
    """Get the active contexts (legacy support endpoint)."""
    from database import db
    contexts = await db.get_global_contexts()
    active_names = [c.get("context_name") for c in contexts if c.get("context_type") == "universal" or c.get("is_active", False)]
    return {"active_context_name": ", ".join(active_names)}


@app.get("/api/orders/confirmed", tags=["Orders"])
async def get_confirmed_orders():
    """Read the list of confirmed customer orders from Supabase."""
    from database import db
    if db.is_configured():
        try:
            db_orders = await db.get_all_orders()
            orders = []
            for order in db_orders:
                orders.append({
                    "name": order.get("name"),
                    "phone": order.get("phone"),
                    "address": order.get("address"),
                    "order_details": order.get("order_details"),
                    "timestamp": order.get("created_at"),
                })
            return {"orders": orders}
        except Exception as e:
            logger.error(f"Error querying orders from Supabase: {e}")
            return JSONResponse(status_code=500, content={"error": f"Failed to query orders from database: {e}"})
    return {"orders": []}


@app.get("/conversations", tags=["Debug"])
async def list_conversations():
    """List active conversations (debug endpoint)."""
    if not settings.DEBUG:
        return JSONResponse(status_code=403, content={"detail": "Debug mode only"})

    sessions = []
    for key, session in conversation_manager._sessions.items():
        sessions.append({
            "key": key,
            "platform": session.platform,
            "user_id": session.user_id,
            "message_count": len(session.messages),
            "human_handoff": session.human_handoff,
            "metadata": session.metadata,
        })

    return {
        "active": len(sessions),
        "sessions": sessions,
    }


@app.post("/test/message", tags=["Debug"])
async def test_message(body: dict):
    """
    Test endpoint — simulate a message without Meta webhooks.

    POST /test/message
    {
        "text": "Show me black t-shirts under 1200",
        "platform": "test",
        "user_id": "test_user_1"
    }
    """
    if not settings.DEBUG:
        return JSONResponse(status_code=403, content={"detail": "Debug mode only"})

    from agent.salesman import get_ai_response

    text = body.get("text", "")
    platform = body.get("platform", "test")
    user_id = body.get("user_id", "test_user")

    if not text:
        return JSONResponse(status_code=400, content={"detail": "text is required"})

    if not settings.ai_reply_ready:
        return JSONResponse(
            status_code=503,
            content={
                "detail": "No AI provider is configured, so this endpoint will not invent a reply.",
                "missing_ai_keys": settings.missing_ai_keys,
                "multi_ai_configured": settings.multi_ai_configured,
                "meta_tokens": {
                    "required": False,
                    "used": False,
                    "page_access_token_set": bool(settings.META_PAGE_ACCESS_TOKEN),
                    "note": (
                        "META_PAGE_ACCESS_TOKEN, WhatsApp tokens, and other Meta secrets "
                        "are not read by /test/message. A missing Meta token is not this error."
                    ),
                },
            },
        )

    # Get or create session
    session = await conversation_manager.get_or_create(platform, user_id)
    await session.add_message("user", text)

    # Get AI response
    response_text, _ = await get_ai_response(
        messages=session.get_chat_messages(),
        platform=platform,
        user_id=user_id,
    )

    await session.add_message("assistant", response_text)

    return {
        "response": response_text,
        "platform": platform,
        "user_id": user_id,
        "message_count": len(session.messages),
        "meta_tokens": {
            "required": False,
            "used": False,
        },
    }


# ── Console & Facebook API Endpoints ─────────────────────────────────────


@app.get("/console", response_class=HTMLResponse, tags=["Console"])
async def get_console():
    """Serve the static test chat console frontend."""
    static_file = os.path.join(os.path.dirname(__file__), "static", "index.html")
    if os.path.exists(static_file):
        with open(static_file, "r") as f:
            return f.read()
    return "<h3>Console HTML file not found</h3>"


@app.get("/api/facebook/conversations", tags=["Console"])
async def api_facebook_conversations():
    """
    Fetch conversations from Facebook Graph API (live inbox),
    or fall back to local sessions in memory if Graph API fails.
    """
    conversations = []
    meta_api_error = False
    error_detail = None

    # Try fetching from Facebook Page Graph API first
    if settings.messenger_configured:
        try:
            page_id = settings.META_PAGE_ID
            access_token = settings.META_PAGE_ACCESS_TOKEN
            url = f"https://graph.facebook.com/v25.0/{page_id}/conversations"
            params = {
                "fields": "id,participants,updated_time,unread_count,messages.limit(1){message,attachments}",
                "access_token": access_token
            }
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.get(url, params=params)

            if response.status_code == 200:
                data = response.json()
                for item in data.get("data", []):
                    part_data = item.get("participants", {}).get("data", [])
                    p_name = "Facebook User"
                    p_id = "unknown"
                    if part_data:
                        other_parts = [p for p in part_data if p.get("id") != page_id]
                        p_info = other_parts[0] if other_parts else part_data[0]
                        p_name = p_info.get("name", "Facebook User")
                        p_id = p_info.get("id", "unknown")

                    last_msg_list = item.get("messages", {}).get("data", [])
                    last_text = "No message text"
                    if last_msg_list:
                        m = last_msg_list[0]
                        last_text = m.get("message", "")
                        if not last_text:
                            # Check if the last message was an image attachment
                            attachments = m.get("attachments", {}).get("data", [])
                            if attachments:
                                last_text = "[Image Attachment]"

                    # Track user in Supabase immediately
                    if p_id != "unknown":
                        from database import db
                        if db.is_configured():
                            await db.create_or_update_user(
                                platform="messenger",
                                user_id=p_id,
                                name=p_name
                            )

                    session = conversation_manager.get("messenger", p_id)
                    is_handoff = session.human_handoff if session else False

                    conversations.append({
                        "user_id": p_id,
                        "platform": "messenger",
                        "participant_name": p_name,
                        "updated_time": item.get("updated_time"),
                        "unread_count": item.get("unread_count", 0),
                        "last_message": last_text,
                        "human_handoff": is_handoff,
                        "conversation_id": item.get("id")
                    })
            else:
                meta_api_error = True
                error_detail = response.json().get("error", {}).get("message", response.text)
        except Exception as e:
            meta_api_error = True
            error_detail = str(e)

    # Combine or fall back to local sessions in conversation manager
    local_sessions = []
    for key, sess in conversation_manager._sessions.items():
        user_id = sess.user_id
        platform = sess.platform

        exists = any(c["user_id"] == user_id and c["platform"] == platform for c in conversations)
        if not exists:
            last_text = "No message history"
            if sess.messages:
                last_msg = sess.messages[-1]
                last_text = last_msg.get("content", "No content")

            local_sessions.append({
                "user_id": user_id,
                "platform": platform,
                "participant_name": sess.metadata.get("user_name") or f"{platform.capitalize()} User",
                "updated_time": time.strftime("%Y-%m-%dT%H:%M:%S+0000", time.gmtime(sess.last_active)),
                "unread_count": 0,
                "last_message": last_text,
                "human_handoff": sess.human_handoff,
                "conversation_id": f"local_{user_id}"
            })

    all_conversations = local_sessions + conversations

    return {
        "meta_api_error": meta_api_error,
        "error_detail": error_detail,
        "conversations": all_conversations
    }


async def fetch_paginated_messages(conversation_id: str, access_token: str, limit: int = 100) -> List[Dict]:
    """Fetch messages from Facebook Graph API with pagination support."""
    messages = []
    # Use v25.0 since it is current and has deprecated v21.0
    url = f"https://graph.facebook.com/v25.0/{conversation_id}/messages"
    params = {
        "fields": "id,message,from,created_time,attachments",
        "limit": min(limit, 50),
        "access_token": access_token
    }

    async with httpx.AsyncClient(timeout=10.0) as client:
        while url and len(messages) < limit:
            try:
                response = await client.get(url, params=params if "?" not in url else {})
                if response.status_code != 200:
                    logger.error(f"Failed to fetch paginated messages: {response.text}")
                    break

                data = response.json()
                page_msgs = data.get("data", [])
                messages.extend(page_msgs)

                # Retrieve the next page URL
                url = data.get("paging", {}).get("next")
                # Clear request parameters since the "next" link already has them
                params = {}

                if not page_msgs:
                    break
            except Exception as e:
                logger.error(f"Error during paginated fetch: {e}")
                break

    return messages


@app.get("/api/facebook/conversations/{conversation_id}/messages", tags=["Console"])
async def api_facebook_messages(
    conversation_id: str,
    platform: str,
    user_id: str,
    force_sync: bool = False,
):
    """
    Fetch messages for a conversation.
    Syncs from Meta Graph API only on first load (empty session) or when
    force_sync=true is passed. Subsequent polls use the in-memory cache so
    the UI stays fast and flicker-free.
    """
    session = await conversation_manager.get_or_create(platform, user_id)

    # Only hit the Facebook Graph API when:
    #  1. The local session has no messages yet (first open), OR
    #  2. The caller explicitly requested a forced re-sync
    should_sync = (
        not conversation_id.startswith("local_")
        and platform == "messenger"
        and settings.messenger_configured
        and (force_sync or len(session.messages) == 0)
    )

    if should_sync:
        try:
            page_id = settings.META_PAGE_ID
            access_token = settings.META_PAGE_ACCESS_TOKEN

            # Fetch last 50 messages only — enough for the console view
            fb_messages = await fetch_paginated_messages(conversation_id, access_token, limit=50)

            # Meta returns newest-first; reverse to chronological order
            fb_messages.reverse()

            # Rebuild the session from live history
            session.messages.clear()
            for msg in fb_messages:
                from_id = msg.get("from", {}).get("id")
                text = msg.get("message", "")
                msg_id = msg.get("id")
                
                # Check for attachments
                attachments = msg.get("attachments", {}).get("data", [])
                image_url = None
                for attach in attachments:
                    mime_type = attach.get("mime_type", "")
                    if "image" in mime_type or attach.get("image_data"):
                        image_url = attach.get("image_data", {}).get("url")
                        if not image_url:
                            image_url = attach.get("payload", {}).get("url")
                        break
                        
                if not text and not image_url:
                    continue

                if image_url:
                    content = [
                        {"type": "text", "text": text or ""},
                        {"type": "image_url", "image_url": {"url": image_url}}
                    ]
                else:
                    content = text

                if from_id == page_id:
                    await session.add_message("assistant", content, message_id=msg_id)
                    session.messages[-1]["sender_type"] = "human"
                else:
                    await session.add_message("user", content, message_id=msg_id)

            logger.info(f"✅ Synced {len(session.messages)} messages for {user_id} from Meta Graph API")
        except Exception as e:
            logger.warning(f"⚠️ Failed to sync message history from Meta for thread {conversation_id}: {e}")

    messages = list(session.messages)
    session_data = {
        "user_id": session.user_id,
        "platform": session.platform,
        "human_handoff": session.human_handoff,
        "last_active": session.last_active,
        "metadata": session.metadata,
    }
    return {
        "source": "local" if not should_sync else "meta",
        "messages": messages,
        "session": session_data,
    }


@app.post("/api/users/update-role", tags=["Console"])
async def update_user_role(body: dict = Body(...)):
    """Update a user's role (Admin, Tester, Customer)."""
    from database import db
    platform = body.get("platform")
    user_id = body.get("user_id")
    role = body.get("role")

    if not platform or not user_id or not role:
        return JSONResponse(status_code=400, content={"error": "platform, user_id, and role are required."})

    if role not in ("Admin", "Tester", "Customer"):
        return JSONResponse(status_code=400, content={"error": "Role must be Admin, Tester, or Customer."})

    # Update DB
    await db.create_or_update_user(
        platform=platform,
        user_id=user_id,
        role=role
    )

    # Update local session in memory if it exists
    session = conversation_manager.get(platform, user_id)
    if session:
        session.metadata["role"] = role

    return {"status": "ok", "role": role}


@app.post("/api/facebook/send", tags=["Console"])
async def api_facebook_send(
    user_id: str = Body(...),
    platform: str = Body(...),
    text: str = Body(...),
    sender: str = Body("human")
):
    """Send a message to a customer and optionally trigger the AI response."""
    session = await conversation_manager.get_or_create(platform, user_id)

    if sender == "human":
        await session.set_human_handoff(True)
        await session.add_message("assistant", text)
        session.messages[-1]["sender_type"] = "human"

        if platform == "messenger" and settings.messenger_configured:
            await messenger_api.send_text(user_id, text)
        elif platform == "whatsapp" and settings.whatsapp_configured:
            from messaging import whatsapp_api
            await whatsapp_api.send_text(user_id, text)

        return {"status": "sent", "sender": "human"}
    else:
        # Simulate incoming user message and trigger AI response
        await session.add_message("user", text)

        from agent.salesman import get_ai_response
        response_text, _ = await get_ai_response(
            messages=session.get_chat_messages(),
            platform=platform,
            user_id=user_id,
        )
        await session.add_message("assistant", response_text)

        if platform == "messenger" and settings.messenger_configured:
            await messenger_api.send_text_chunked(user_id, response_text)
        elif platform == "whatsapp" and settings.whatsapp_configured:
            from messaging import whatsapp_api
            await whatsapp_api.send_text(user_id, response_text)

        return {"status": "processed", "response": response_text, "sender": "ai"}


@app.post("/api/facebook/conversations/{platform}/{user_id}/handoff", tags=["Console"])
async def api_facebook_handoff(platform: str, user_id: str, body: dict):
    """Toggle human handoff status for a conversation."""
    session = conversation_manager.get(platform, user_id)
    if not session:
        return JSONResponse(status_code=404, content={"detail": "Session not found"})
    await session.set_human_handoff(body.get("human_handoff", True))
    return {"status": "ok", "human_handoff": session.human_handoff}


@app.post("/api/facebook/conversations/{platform}/{user_id}/reset", tags=["Console"])
async def api_facebook_reset(platform: str, user_id: str):
    """Reset session history."""
    session = conversation_manager.get(platform, user_id)
    if not session:
        return JSONResponse(status_code=404, content={"detail": "Session not found"})
    await session.reset()
    return {"status": "reset"}


@app.post("/api/facebook/conversations/{platform}/{user_id}/ai-respond", tags=["Console"])
async def api_facebook_ai_respond(platform: str, user_id: str):
    """Force the AI agent to generate a response."""
    session = conversation_manager.get(platform, user_id)
    if not session:
        return JSONResponse(status_code=404, content={"detail": "Session not found"})

    from agent.salesman import get_ai_response
    response_text, _ = await get_ai_response(
        messages=session.get_chat_messages(),
        platform=platform,
        user_id=user_id,
    )
    await session.add_message("assistant", response_text)

    if platform == "messenger" and settings.messenger_configured:
        await messenger_api.send_text_chunked(user_id, response_text)
    elif platform == "whatsapp" and settings.whatsapp_configured:
        from messaging import whatsapp_api
        await whatsapp_api.send_text(user_id, response_text)

    return {"status": "ok", "response": response_text}


@app.post("/api/facebook/simulate", tags=["Console"])
async def api_facebook_simulate(
    name: str = Body(...),
    platform: str = Body(...),
    user_id: str = Body(None),
    initial_message: str = Body(None),
    role: str = Body("Customer")
):
    """Simulate a new customer conversation with a specified role."""
    import random
    uid = user_id or str(random.randint(100000000000000, 999999999999999))
    session = await conversation_manager.get_or_create(platform, uid, user_name=name)

    # Update user role in Supabase and local session
    from database import db
    if db.is_configured():
        await db.create_or_update_user(
            platform=platform,
            user_id=uid,
            role=role
        )
    session.metadata["role"] = role

    msg = initial_message or "Hello!"
    await session.add_message("user", msg)

    from agent.salesman import get_ai_response
    response_text, _ = await get_ai_response(
        messages=session.get_chat_messages(),
        platform=platform,
        user_id=uid,
    )
    await session.add_message("assistant", response_text)

    return {
        "status": "simulated",
        "user_id": uid,
        "platform": platform,
        "name": name
    }


# ── Entry Point ──────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn

    print(f"""
╔══════════════════════════════════════════════════════════╗
║            🤖 AI Salesman for {settings.STORE_NAME:<20}    ║
╠══════════════════════════════════════════════════════════╣
║                                                          ║
║  Endpoints:                                              ║
║    GET  /                         → Health check         ║
║    GET  /health                   → Detailed status      ║
║    GET  /docs                     → Swagger UI           ║
║    POST /test/message             → Test AI (debug)      ║
║                                                          ║
║  Webhooks:                                               ║
║    GET  /webhook/messenger        → Verify               ║
║    POST /webhook/messenger        → Receive messages     ║
║    GET  /webhook/whatsapp         → Verify               ║
║    POST /webhook/whatsapp         → Receive messages     ║
║                                                          ║
╚══════════════════════════════════════════════════════════╝
    """)

    uvicorn.run(
        "main:app",
        host=settings.HOST,
        port=settings.PORT,
        reload=settings.DEBUG,
    )
