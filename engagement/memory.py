"""In-process dedupe and rate limits for Page engagement."""

from __future__ import annotations

import time
from typing import Dict, List


class EngagementMemory:
    def __init__(self) -> None:
        self.replied: set[str] = set()
        self.liked: set[str] = set()
        self.replies_by_post: Dict[str, List[float]] = {}
        self.like_times: List[float] = []

    def clear(self) -> None:
        self.replied.clear()
        self.liked.clear()
        self.replies_by_post.clear()
        self.like_times.clear()

    def recent_replies(self, post_id: str, now: float) -> int:
        times = [t for t in self.replies_by_post.get(post_id, []) if now - t < 3600]
        self.replies_by_post[post_id] = times
        return len(times)

    def recent_likes(self, now: float) -> int:
        self.like_times = [t for t in self.like_times if now - t < 3600]
        return len(self.like_times)

    def claim_reply(self, comment_id: str, post_id: str, now: float, limit: int) -> bool:
        if not comment_id or comment_id in self.replied:
            return False
        if self.recent_replies(post_id or comment_id, now) >= limit:
            return False
        self.replied.add(comment_id)
        self.replies_by_post.setdefault(post_id or comment_id, []).append(now)
        return True

    def claim_like(self, comment_id: str, now: float, limit: int) -> bool:
        if not comment_id or comment_id in self.liked:
            return False
        if self.recent_likes(now) >= limit:
            return False
        self.liked.add(comment_id)
        self.like_times.append(now)
        return True


memory = EngagementMemory()
