from __future__ import annotations

from typing import Optional, Tuple, Dict, Any
from conversation.manager import ConversationSession

class BaseCommandHandler:
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
        """
        Executes the command.
        
        Returns:
            (handled_bool, reply_text_or_none)
            If handled_bool is True, execution stops and the webhook returns reply_text_or_none.
            If handled_bool is False, the webhook passes the message to the AI.
        """
        raise NotImplementedError("Command handlers must implement the execute method.")
