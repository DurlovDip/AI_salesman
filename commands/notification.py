from __future__ import annotations

import logging
from typing import Optional, Tuple, Dict, Any
from conversation.manager import ConversationSession
from commands.base import BaseCommandHandler
from database import db

logger = logging.getLogger(__name__)

class NotificationCommandHandler(BaseCommandHandler):
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
        try:
            admins = await db.get_admin_users()
            if not admins:
                # Default fallback admin
                admins = [{"platform": "messenger", "user_id": "26761204070240994", "name": "Dip Durlov"}]
            
            notification_msg = f"🔔 AI Notification Alert: The AI Salesman requested attention for customer {user_id} on {platform}."
            for admin in admins:
                adm_plat = admin.get("platform")
                adm_uid = admin.get("user_id")
                if adm_plat and adm_uid:
                    from agent.tool_executor import _send_platform_text
                    await _send_platform_text(adm_plat, adm_uid, notification_msg)
        except Exception as e:
            logger.error(f"Failed to send admin notification: {e}")
            
        return True, None
