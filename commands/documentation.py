from __future__ import annotations

import logging
from typing import Optional, Tuple, Dict, Any
from conversation.manager import ConversationSession
from commands.base import BaseCommandHandler
from database import db

logger = logging.getLogger(__name__)

class DocumentationCommandHandler(BaseCommandHandler):
    async def execute(
        self,
        session: ConversationSession,
        platform: str,
        user_id: str,
        command_record: Dict[str, Any],
        arguments: Dict[str, str],
        user_text: str,
        user_name: str,
        user_role: str,
    ) -> Tuple[bool, Optional[str]]:
        cmd_name = command_record.get("command")
        cmd_type = command_record.get("command_type")
        current_mode = session.metadata.get("current_mode")

        if cmd_type == "initiator":
            session.metadata["current_mode"] = "documentation"
            session.metadata["mode_state"] = {}
            if db.is_configured():
                await db.create_or_update_user(platform=platform, user_id=user_id, metadata=session.metadata)
            logger.info("📚 Documentation mode initiated.")
            # Let the message pass to AI so it can compile the command documentation
            return False, None

        if current_mode == "documentation":
            if cmd_name in ("doc_response", "doc_terminate") or cmd_type == "terminator":
                session.metadata.pop("current_mode", None)
                session.metadata.pop("mode_state", None)
                if db.is_configured():
                    await db.create_or_update_user(platform=platform, user_id=user_id, metadata=session.metadata)
                logger.info("📚 Documentation mode terminated by AI.")
                return True, None

        return False, None
