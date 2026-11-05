"""
Decide whether a Page feed comment deserves a reply or a like.

Rules stay conservative on purpose: public comments are visible, so the bot
answers real questions and likes a customer's comment once. It does not reply
to itself, pile onto nested threads, or like complaints.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from engagement.memory import EngagementMemory

MAX_REPLIES_PER_POST_PER_HOUR = 6
MAX_LIKES_PER_HOUR = 30

_QUESTION_HINTS = (
    "?",
    "price",
    "koto",
    "dam",
    "size",
    "order",
    "delivery",
    "available",
    "stock",
    "color",
    "colour",
    "kothay",
    "shipping",
    "return",
    "কত",
    "দাম",
    "সাইজ",
    "অর্ডার",
    "ডেলিভারি",
)
_NEGATIVE = ("scam", "fake", "fraud", "worst", "trash", "spam", "horrible")
_EMOJI_ONLY = re.compile(
    r"^[\s\W_]+$",
    re.UNICODE,
)


@dataclass(frozen=True)
class CommentDecision:
    reply: bool
    like: bool
    reason: str


def _is_top_level(value: dict) -> bool:
    parent_id = str(value.get("parent_id") or "")
    post_id = str(value.get("post_id") or "")
    if not parent_id:
        return True
    return bool(post_id) and parent_id == post_id


def decide_comment(
    value: dict,
    page_id: str,
    memory: EngagementMemory,
    now: float | None = None,
) -> CommentDecision:
    """Read-only decision. The handler records the action after it succeeds."""
    import time

    now = time.time() if now is None else now
    author = str((value.get("from") or {}).get("id") or "")
    message = (value.get("message") or "").strip()
    comment_id = str(value.get("comment_id") or "")
    post_id = str(value.get("post_id") or "")

    if value.get("item") not in (None, "comment"):
        return CommentDecision(False, False, "not a comment")
    if value.get("verb") not in (None, "add"):
        return CommentDecision(False, False, "ignoring non-add verb")
    if page_id and author == str(page_id):
        return CommentDecision(False, False, "comment is from the Page itself")
    if not _is_top_level(value):
        return CommentDecision(False, False, "nested reply skipped to avoid thread spam")
    if not message:
        return CommentDecision(False, False, "empty comment")

    lowered = message.lower()
    negative = any(word in lowered for word in _NEGATIVE)
    emoji_only = _EMOJI_ONLY.match(message) is not None
    question = (not emoji_only) and any(hint in lowered for hint in _QUESTION_HINTS)
    substantive = len(re.sub(r"\s+", "", message)) >= 12

    already_replied = comment_id in memory.replied
    over_reply_limit = memory.recent_replies(post_id or comment_id, now) >= MAX_REPLIES_PER_POST_PER_HOUR
    already_liked = comment_id in memory.liked
    over_like_limit = memory.recent_likes(now) >= MAX_LIKES_PER_HOUR

    reply = (question or (substantive and not emoji_only)) and not already_replied and not over_reply_limit
    like = (not negative) and (not already_liked) and (not over_like_limit)

    if already_replied and already_liked:
        reason = "already handled"
    elif over_reply_limit and reply is False and question:
        reason = "reply rate limit reached for this post"
    elif not reply and emoji_only:
        reason = "emoji or reaction-style comment; like only"
    elif not reply and not question and not substantive:
        reason = "too short to answer publicly"
    elif negative and reply:
        reason = "question on a negative comment; reply without liking"
    else:
        reason = "customer comment"

    if not reply and not like:
        if already_replied or already_liked:
            reason = "already handled"
        elif over_like_limit and not reply:
            reason = "like rate limit reached"

    return CommentDecision(reply, like, reason)
