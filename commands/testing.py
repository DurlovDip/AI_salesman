from __future__ import annotations

import logging
from typing import Optional, Tuple, Dict, Any
from conversation.manager import ConversationSession
from commands.base import BaseCommandHandler
from database import db

logger = logging.getLogger(__name__)

class TestingModeHandler(BaseCommandHandler):
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
            session.metadata["current_mode"] = "testing"
            session.metadata["mode_state"] = {"draft": {}, "awaiting": None}
            current_mode = "testing"

        if current_mode == "testing":
            # Update draft with all available mediator commands present in arguments
            for param in ["title", "context", "type"]:
                if param in arguments:
                    session.metadata["mode_state"]["draft"][param] = arguments[param]
                    if session.metadata["mode_state"].get("awaiting") == param:
                        session.metadata["mode_state"]["awaiting"] = None

            if cmd_type == "mediator":
                if cmd_name == "end":
                    session.metadata.pop("current_mode", None)
                    session.metadata.pop("mode_state", None)
                    if db.is_configured():
                        await db.create_or_update_user(platform=platform, user_id=user_id, metadata=session.metadata)
                    logger.info("🧪 Testing mode dismissed by tester @end.")
                    return True, "Testing mode cancelled. Context creation dismissed."
                session.metadata["mode_state"]["awaiting"] = None

            # Handle plain response text if we are in testing mode and awaiting a parameter
            elif not command_record.get("command_id") and session.metadata["mode_state"].get("awaiting"):
                awaiting = session.metadata["mode_state"]["awaiting"]
                from tester_commands import clean_plain_input
                clean_text = clean_plain_input(user_text, f"@{awaiting}")
                session.metadata["mode_state"]["draft"][awaiting] = clean_text
                session.metadata["mode_state"]["awaiting"] = None

            # Check terminator
            if cmd_name == "test_terminate":
                session.metadata.pop("current_mode", None)
                session.metadata.pop("mode_state", None)
                if db.is_configured():
                    await db.create_or_update_user(platform=platform, user_id=user_id, metadata=session.metadata)
                logger.info("🧪 Testing mode terminated by AI.")
                return True, None

            # Evaluate state machine rules
            draft = session.metadata["mode_state"]["draft"]
            
            if not draft.get("title"):
                session.metadata["mode_state"]["awaiting"] = "title"
                if db.is_configured():
                    await db.create_or_update_user(platform=platform, user_id=user_id, metadata=session.metadata)
                return True, "Please enter the title for the new context:"

            if not draft.get("context"):
                session.metadata["mode_state"]["awaiting"] = "context"
                if db.is_configured():
                    await db.create_or_update_user(platform=platform, user_id=user_id, metadata=session.metadata)
                return True, "Please enter the context rules/text:"

            # Both parameters are present. Save to database!
            title = draft["title"]
            context_text = draft["context"]
            context_type = draft.get("type", "universal")
            if context_type not in ("universal", "special"):
                context_type = "universal"

            # Clear state
            session.metadata.pop("current_mode", None)
            session.metadata.pop("mode_state", None)
            
            if db.is_configured():
                await db.create_or_update_user(platform=platform, user_id=user_id, metadata=session.metadata)

            try:
                # Save to database
                await db.save_global_context(
                    name=title,
                    text=context_text,
                    description=f"Created by tester {user_name} ({platform}:{user_id})",
                    context_type=context_type,
                    is_active=(context_type == "universal"),
                )

                # Re-compile guidelines
                from main import compile_and_save_active_guidelines
                await compile_and_save_active_guidelines()

                return True, "@confirmation\nContext is added & thanks for making me better"
            except Exception as e:
                logger.error(f"Failed to save context from tester command: {e}")
                return True, f"❌ Failed to add context '{title}'.\nError: {str(e)}\n\nType @testing to try again."

        return False, None
