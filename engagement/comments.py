"""Turn a Page feed webhook value into at most one reply and one like."""

from __future__ import annotations

import logging
import re
import time
from typing import Any, Dict

from config import settings
from engagement.memory import memory
from engagement.policy import MAX_LIKES_PER_HOUR, MAX_REPLIES_PER_POST_PER_HOUR, decide_comment
from messaging.messenger_api import clean_markdown
from messaging import page_api

logger = logging.getLogger(__name__)

_IMAGE_TAG = re.compile(r"\[IMAGE:\s*https?://[^\]]+\]")


def to_public_comment(text: str) -> str:
    """Keep a public reply to a single short plain-text comment."""
    cleaned = _IMAGE_TAG.sub("", text or "")
    cleaned = clean_markdown(cleaned)
    cleaned = " ".join(cleaned.split())
    return cleaned[:700].strip()


async def handle_feed_value(value: Dict[str, Any], page_id: str) -> Dict[str, Any]:
    """
    Handle one Page `feed` change.

    Safe defaults: if both engagement flags are off, this returns without
    calling Graph or the AI. Reaction events are logged and never answered
    with another reaction.
    """
    page_id = str(page_id or settings.META_PAGE_ID or "")
    if settings.META_PAGE_ID and page_id and page_id != str(settings.META_PAGE_ID):
        return {"action": "ignored", "reason": "event is for a different Page"}

    item = value.get("item")
    verb = value.get("verb")
    if item == "reaction":
        logger.info(
            "Page reaction %s (%s) on %s; not reacting back",
            verb,
            value.get("reaction_type"),
            value.get("post_id") or value.get("comment_id"),
        )
        return {
            "action": "logged",
            "reason": "reaction events are not answered with another reaction",
        }

    if item not in (None, "comment") or verb not in (None, "add"):
        return {"action": "ignored", "reason": f"unsupported feed event {item}/{verb}"}

    if not settings.page_comments_enabled and not settings.page_reactions_enabled:
        return {
            "action": "disabled",
            "reason": "ENABLE_PAGE_COMMENTS and ENABLE_PAGE_REACTIONS are false",
        }

    decision = decide_comment(value, page_id, memory, now=time.time())
    comment_id = str(value.get("comment_id") or "")
    post_id = str(value.get("post_id") or "")
    result: Dict[str, Any] = {
        "action": "considered",
        "reason": decision.reason,
        "reply": False,
        "like": False,
        "comment_id": comment_id,
    }

    if decision.like and settings.page_reactions_enabled:
        if memory.claim_like(comment_id, time.time(), MAX_LIKES_PER_HOUR):
            liked = await page_api.like_object(comment_id)
            result["like"] = "error" not in liked
            if not result["like"]:
                result["like_error"] = liked.get("error")
    elif decision.like:
        result["like_skipped"] = "ENABLE_PAGE_REACTIONS is false"

    if decision.reply and settings.page_comments_enabled:
        if not settings.ai_reply_ready:
            result["reply_skipped"] = (
                "AI keys missing. Refusing to post a canned public comment. "
                "Meta token is present or not is a separate check."
            )
            logger.warning("Skipping comment reply for %s: no AI key", comment_id)
        elif memory.claim_reply(comment_id, post_id, time.time(), MAX_REPLIES_PER_POST_PER_HOUR):
            reply_text = await _draft_reply(value, page_id)
            if not reply_text:
                result["reply_skipped"] = "AI returned an empty public reply"
            else:
                posted = await page_api.reply_to_comment(comment_id, reply_text)
                result["reply"] = "error" not in posted
                if not result["reply"]:
                    result["reply_error"] = posted.get("error")
    elif decision.reply:
        result["reply_skipped"] = "ENABLE_PAGE_COMMENTS is false"

    return result


async def _draft_reply(value: Dict[str, Any], page_id: str) -> str:
    from agent.salesman import get_ai_response
    from conversation.manager import conversation_manager

    author = value.get("from") or {}
    author_id = str(author.get("id") or "page_commenter")
    author_name = author.get("name") or "Customer"
    comment = (value.get("message") or "").strip()
    session = await conversation_manager.get_or_create("page_comment", author_id, author_name)
    prompt = (
        f"A customer wrote this public comment on a {settings.STORE_NAME} Facebook Page post. "
        f"Comment: {comment}\n"
        "Write one short public reply in the customer's language. "
        "Do not collect a phone number, address, or payment details in the comment."
    )
    await session.add_message("user", prompt)
    text, _ = await get_ai_response(
        messages=session.get_chat_messages(),
        platform="page_comment",
        user_id=author_id,
        use_tools=False,
    )
    public = to_public_comment(text)
    if public:
        await session.add_message("assistant", public)
    return public
