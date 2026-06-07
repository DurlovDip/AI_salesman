from __future__ import annotations

import logging
from typing import Optional, Tuple, Dict, Any
from conversation.manager import ConversationSession
from commands.base import BaseCommandHandler
from database import db

logger = logging.getLogger(__name__)

class SetDomainCommandHandler(BaseCommandHandler):
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
            session.metadata["current_mode"] = "setdomain"
            session.metadata["mode_state"] = {"awaiting": "value", "draft": {}}
            current_mode = "setdomain"

        if current_mode == "setdomain":
            # Update value draft from arguments if present
            if "domain" in arguments:
                session.metadata["mode_state"]["draft"]["value"] = arguments["domain"]
                session.metadata["mode_state"]["awaiting"] = None

            if cmd_type == "mediator" and cmd_name == "end":
                session.metadata.pop("current_mode", None)
                session.metadata.pop("mode_state", None)
                if db.is_configured():
                    await db.create_or_update_user(platform=platform, user_id=user_id, metadata=session.metadata)
                logger.info("⚙️ Setdomain mode cancelled via @end.")
                return True, "Setdomain mode cancelled."

            # Handle plain response text
            elif not command_record.get("command_id") and session.metadata["mode_state"].get("awaiting") == "value":
                from tester_commands import clean_plain_input
                clean_text = clean_plain_input(user_text, "@domain").strip()
                session.metadata["mode_state"]["draft"]["value"] = clean_text
                session.metadata["mode_state"]["awaiting"] = None

            # Check terminator
            if cmd_name == "domaindone" or cmd_type == "terminator":
                session.metadata.pop("current_mode", None)
                session.metadata.pop("mode_state", None)
                if db.is_configured():
                    await db.create_or_update_user(platform=platform, user_id=user_id, metadata=session.metadata)
                logger.info("⚙️ Setdomain mode terminated by AI.")
                return True, None

            # Process state machine logic
            draft = session.metadata["mode_state"]["draft"]
            val = draft.get("value")

            if not val:
                session.metadata["mode_state"]["awaiting"] = "value"
                if db.is_configured():
                    await db.create_or_update_user(platform=platform, user_id=user_id, metadata=session.metadata)
                return True, "Please enter the domain value (1 = Admin, 2 = Admin/Tester, 3 = All):"

            if val not in ("1", "2", "3"):
                session.metadata["mode_state"]["awaiting"] = "value"
                draft["value"] = None
                if db.is_configured():
                    await db.create_or_update_user(platform=platform, user_id=user_id, metadata=session.metadata)
                return True, "Invalid domain value. Please enter 1, 2, or 3:"

            # Valid domain value. Save it!
            session.metadata.pop("current_mode", None)
            session.metadata.pop("mode_state", None)
            if db.is_configured():
                await db.create_or_update_user(platform=platform, user_id=user_id, metadata=session.metadata)

            try:
                await db.save_reply_domain(val)
                logger.info(f"⚙️ Global reply domain set to: {val}")
                return True, f"@domaindone\nDomain is set successfully to {val}."
            except Exception as e:
                logger.error(f"Failed to save reply domain: {e}")
                return True, f"Failed to save domain setting. Error: {str(e)}."

        return False, None
