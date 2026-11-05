"""
Facebook Page feed webhook
==========================
GET  /webhook/page — Meta verification challenge
POST /webhook/page — Page feed changes (comments, reactions)

Subscribe the Page webhook field `feed`. Messaging still belongs on
/webhook/messenger. Both URLs can accept a feed change without double work
as long as Meta is pointed at only one of them.
"""

from __future__ import annotations

import json
import logging

from fastapi import APIRouter, HTTPException, Query, Request, Response

from config import settings
from engagement.comments import handle_feed_value
from webhooks.signature import verify_meta_signature

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/webhook", tags=["Page Webhook"])


@router.get("/page")
async def verify_page(
    hub_mode: str = Query(None, alias="hub.mode"),
    hub_verify_token: str = Query(None, alias="hub.verify_token"),
    hub_challenge: str = Query(None, alias="hub.challenge"),
):
    if hub_mode == "subscribe" and hub_verify_token == settings.META_VERIFY_TOKEN:
        logger.info("Page webhook verified")
        return Response(content=hub_challenge or "", media_type="text/plain")

    logger.warning("Page webhook verification failed (mode=%s)", hub_mode)
    raise HTTPException(status_code=403, detail="Verification failed")


@router.post("/page")
async def handle_page(request: Request):
    body = await request.body()
    if settings.META_APP_SECRET:
        signature = request.headers.get("X-Hub-Signature-256", "")
        if not verify_meta_signature(body, signature):
            logger.warning("Invalid Page webhook signature")
            raise HTTPException(status_code=403, detail="Invalid signature")

    data = json.loads(body)
    if data.get("object") != "page":
        return {"status": "ignored"}

    handled = []
    for entry in data.get("entry", []):
        page_id = str(entry.get("id") or settings.META_PAGE_ID or "")
        for change in entry.get("changes", []):
            if change.get("field") != "feed":
                continue
            handled.append(await handle_feed_value(change.get("value") or {}, page_id))

    if not handled and not settings.page_comments_enabled and not settings.page_reactions_enabled:
        return {"status": "disabled", "channel": "page"}

    return {"status": "ok", "handled": handled}
