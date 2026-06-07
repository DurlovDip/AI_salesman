from __future__ import annotations

from typing import Optional
from commands.base import BaseCommandHandler
from commands.notification import NotificationCommandHandler
from commands.testing import TestingModeHandler
from commands.documentation import DocumentationCommandHandler

# Map command names to singleton handler instances
_doc_handler = DocumentationCommandHandler()
_registry = {
    "notification": NotificationCommandHandler(),
    "testing": TestingModeHandler(),
    "title": TestingModeHandler(),
    "context": TestingModeHandler(),
    "type": TestingModeHandler(),
    "end": TestingModeHandler(),
    "test_terminate": TestingModeHandler(),
    "test_termination": TestingModeHandler(),
    "doc": _doc_handler,
    "doc_response": _doc_handler,
    "doc_terminate": _doc_handler,
}

def get_handler(command_name: str) -> Optional[BaseCommandHandler]:
    """Resolve command name to its modular handler class."""
    return _registry.get(command_name)
