"""
Conversation Manager
====================
Tracks per-user conversation history and session state.
Platform-agnostic — works for both Messenger and WhatsApp.

Storage: in-memory dict with async persistence to Supabase.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Dict, List, Optional

from config import settings

logger = logging.getLogger(__name__)


class ConversationSession:
    """One user's active conversation with Supabase integration."""

    def __init__(
        self,
        user_id: str,
        platform: str,
        messages: Optional[List[Dict]] = None,
        last_active: Optional[float] = None,
        metadata: Optional[Dict] = None,
        human_handoff: bool = False,
    ) -> None:
        self.user_id = user_id
        self.platform = platform
        self.messages = messages if messages is not None else []
        self.last_active = last_active if last_active is not None else time.time()
        self.metadata = metadata if metadata is not None else {}
        self._human_handoff = human_handoff

    @property
    def is_expired(self) -> bool:
        return (time.time() - self.last_active) > (
            settings.SESSION_TIMEOUT_MINUTES * 60
        )

    @property
    def human_handoff(self) -> bool:
        return self._human_handoff

    @human_handoff.setter
    def human_handoff(self, value: bool) -> None:
        self._human_handoff = value
        # Sync handoff to Supabase in background
        from database import db
        if db.is_configured():
            asyncio.create_task(
                db.create_or_update_user(
                    platform=self.platform,
                    user_id=self.user_id,
                    human_handoff=value
                )
            )

    def add_message(self, role: str, content: str, message_id: Optional[str] = None) -> None:
        """Add a message, trim history, and sync to Supabase."""
        msg = {"role": role, "content": content}
        if message_id:
            msg["message_id"] = message_id
        self.messages.append(msg)
        self.last_active = time.time()

        # Keep only the most recent messages to fit context window
        max_msgs = settings.MAX_HISTORY_MESSAGES
        if len(self.messages) > max_msgs:
            self.messages = self.messages[-max_msgs:]

        # Sync to Supabase in background
        from database import db
        if db.is_configured():
            asyncio.create_task(
                db.save_message(
                    platform=self.platform,
                    user_id=self.user_id,
                    role=role,
                    content=content,
                    message_id=message_id
                )
            )


    def add_tool_messages(self, tool_messages: List[Dict]) -> None:
        """Add tool call + result messages from the AI agent and sync to Supabase."""
        self.messages.extend(tool_messages)
        self.last_active = time.time()

        max_msgs = settings.MAX_HISTORY_MESSAGES
        if len(self.messages) > max_msgs:
            self.messages = self.messages[-max_msgs:]

        # Sync to Supabase in background
        from database import db
        if db.is_configured():
            for msg in tool_messages:
                role = msg.get("role")
                content = msg.get("content")
                name = msg.get("name")
                tool_calls = msg.get("tool_calls")
                tool_call_id = msg.get("tool_call_id")
                asyncio.create_task(
                    db.save_message(
                        platform=self.platform,
                        user_id=self.user_id,
                        role=role,
                        content=content,
                        name=name,
                        tool_calls=tool_calls,
                        tool_call_id=tool_call_id
                    )
                )

    def get_chat_messages(self) -> List[Dict]:
        """Return messages formatted for the AI chat API."""
        return list(self.messages)

    def reset(self) -> None:
        """Clear conversation history (in-memory and handoff state)."""
        self.messages = []
        self.last_active = time.time()
        self.human_handoff = False


class ConversationManager:
    """
    Manages conversation sessions for all users.

    Key format: "{platform}:{user_id}"
    Example: "messenger:123456789" or "whatsapp:8801712345678"
    """

    def __init__(self) -> None:
        self._sessions: Dict[str, ConversationSession] = {}

    def _key(self, platform: str, user_id: str) -> str:
        return f"{platform}:{user_id}"

    async def get_or_create(
        self, platform: str, user_id: str, user_name: Optional[str] = None
    ) -> ConversationSession:
        """Get existing session or load/create a new one asynchronously."""
        key = self._key(platform, user_id)
        session = self._sessions.get(key)

        # Create/load new session or reset expired ones
        if session is None or session.is_expired:
            db_metadata = {"user_name": user_name, "role": "Customer"} if user_name else {"role": "Customer"}
            db_messages = []
            db_handoff = False
 
            # Try to load session from Supabase
            from database import db
            if db.is_configured():
                try:
                    user_data = await db.get_user(platform, user_id)
                    if user_data:
                        # Restore cached user details
                        db_metadata = user_data.get("metadata") or {}
                        # Always keep session metadata role in sync with database role column
                        db_metadata["role"] = user_data.get("role") or db_metadata.get("role") or "Customer"
                        if user_data.get("name"):
                            db_metadata["user_name"] = user_data.get("name")
                        
                        db_handoff = user_data.get("human_handoff", False)
                        
                        # Load recent message history
                        db_messages = await db.get_messages(
                             platform, 
                             user_id, 
                             limit=settings.MAX_HISTORY_MESSAGES
                        )
                        logger.info(f"💾 Loaded session for {key} from Supabase ({len(db_messages)} messages)")
                    else:
                        # Create new user in DB
                        await db.create_or_update_user(
                            platform, 
                            user_id, 
                            name=user_name,
                            metadata=db_metadata,
                            role="Customer"
                        )
                except Exception as e:
                    logger.error(f"Error loading session {key} from Supabase: {e}")

            session = ConversationSession(
                user_id=user_id,
                platform=platform,
                messages=db_messages,
                metadata=db_metadata,
                human_handoff=db_handoff,
            )
            self._sessions[key] = session

        return session

    def get(self, platform: str, user_id: str) -> Optional[ConversationSession]:
        """Get existing session in memory (None if not found or expired)."""
        key = self._key(platform, user_id)
        session = self._sessions.get(key)
        if session and session.is_expired:
            del self._sessions[key]
            return None
        return session

    def remove(self, platform: str, user_id: str) -> None:
        """Remove a session."""
        key = self._key(platform, user_id)
        self._sessions.pop(key, None)

    def cleanup_expired(self) -> int:
        """Remove all expired sessions. Returns count removed."""
        expired_keys = [
            k for k, s in self._sessions.items() if s.is_expired
        ]
        for k in expired_keys:
            del self._sessions[k]
        return len(expired_keys)

    @property
    def active_count(self) -> int:
        return len(self._sessions)


# Global singleton
conversation_manager = ConversationManager()
