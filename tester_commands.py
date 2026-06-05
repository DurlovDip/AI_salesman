"""
Tester Command Handler
======================
Intercepts messages from users with the "Tester" role that start with or contain "@testing" (allowing typos).
Provides a conversational state machine flow to gather title and context, defaulting type to universal.
"""

from __future__ import annotations

import logging
import re
from typing import Optional, Tuple, Dict, Any

logger = logging.getLogger(__name__)


def levenshtein_distance(s1: str, s2: str) -> int:
    """Compute the Levenshtein distance between two strings."""
    if len(s1) < len(s2):
        return levenshtein_distance(s2, s1)
    if len(s2) == 0:
        return len(s1)

    previous_row = range(len(s2) + 1)
    for i, c1 in enumerate(s1):
        current_row = [i + 1]
        for j, c2 in enumerate(s2):
            insertions = previous_row[j + 1] + 1
            deletions = current_row[j] + 1
            substitutions = previous_row[j] + (c1 != c2)
            current_row.append(min(insertions, deletions, substitutions))
        previous_row = current_row

    return previous_row[-1]


def parse_message_tags(text: str) -> Dict[str, Any]:
    """
    Fuzzy parse testing tags from the message.
    Looks for tokens starting with @ and maps them to standard tags.
    """
    targets = {
        "testing": ("@testing", 2),
        "title": ("@title", 2),
        "type": ("@type", 1),
        "context": ("@context", 2),
    }

    matches = []
    # Find all words starting with @
    for m in re.finditer(r'@\w+', text):
        word = m.group(0).lower()
        for key, (target, max_dist) in targets.items():
            if levenshtein_distance(word, target) <= max_dist:
                matches.append({
                    "key": key,
                    "start": m.start(),
                    "end": m.end(),
                    "word": m.group(0)
                })
                break  # Avoid matching multiple targets

    # Sort matches by start index
    matches.sort(key=lambda x: x["start"])

    parsed = {
        "testing_present": False,
        "title": None,
        "type": None,
        "context": None
    }

    for i, match in enumerate(matches):
        key = match["key"]
        if key == "testing":
            parsed["testing_present"] = True

        # Extract content after the tag up to the next tag or end of string
        start_content = match["end"]
        end_content = matches[i + 1]["start"] if i + 1 < len(matches) else len(text)
        content = text[start_content:end_content].strip()

        # Clean up leading separator characters like -, :, =
        content = re.sub(r'^[:\-\s=]+', '', content).strip()

        if key != "testing" and content:
            parsed[key] = content

    return parsed


def clean_plain_input(text: str, tag_to_clean: str) -> str:
    """
    Clean any leading tags or separators if the user prefix-typed them
    in their response (e.g., "@title - My Title" -> "My Title").
    """
    cleaned = text.strip()
    words = cleaned.split()
    if words:
        first_word = words[0]
        if first_word.startswith("@") and levenshtein_distance(first_word.lower(), tag_to_clean.lower()) <= 2:
            cleaned = cleaned[len(first_word):].strip()

    # Clean leading separators
    cleaned = re.sub(r'^[:\-\s=]+', '', cleaned).strip()
    return cleaned


async def handle_tester_command(
    platform: str,
    user_id: str,
    user_text: str,
    user_name: str = "Tester",
) -> Tuple[bool, Optional[str]]:
    """
    Check if the message is part of a conversational @testing command flow from a Tester.
    """
    from database import db
    from conversation.manager import conversation_manager

    # 1. Check if user has the Tester role
    user_data = await db.get_user(platform, user_id)
    user_role = "Customer"
    if user_data:
        user_role = user_data.get("role") or user_data.get("metadata", {}).get("role", "Customer")

    if user_role != "Tester":
        return (False, None)

    # Get the conversation session
    session = await conversation_manager.get_or_create(platform, user_id, user_name)

    # 2. Parse the current message for any tags
    parsed = parse_message_tags(user_text)

    # A trigger is a new fuzzy @testing tag
    is_new_trigger = parsed["testing_present"]

    # Get state from metadata
    tester_state = session.metadata.get("tester_state")
    tester_draft = session.metadata.get("tester_draft") or {}

    if is_new_trigger:
        # Reset draft and start a new context flow
        tester_state = None
        tester_draft = {}

    # If we aren't in the middle of a flow and there's no new trigger, pass to normal AI
    if not is_new_trigger and not tester_state:
        return (False, None)

    # Extract any explicit tags sent in the message
    if parsed["title"]:
        tester_draft["title"] = parsed["title"]
    if parsed["context"]:
        tester_draft["context"] = parsed["context"]
    if parsed["type"]:
        tester_draft["type"] = parsed["type"]

    # If in a conversational step, fill in the corresponding draft field
    if not is_new_trigger:
        if tester_state == "awaiting_title":
            title = clean_plain_input(user_text, "@title")
            if title:
                tester_draft["title"] = title
                tester_state = None
        elif tester_state == "awaiting_context":
            context = clean_plain_input(user_text, "@context")
            if context:
                tester_draft["context"] = context
                tester_state = None

    # Check for missing details
    missing_title = not tester_draft.get("title")
    missing_context = not tester_draft.get("context")

    if missing_title:
        session.metadata["tester_state"] = "awaiting_title"
        session.metadata["tester_draft"] = tester_draft
        
        # Persist session metadata updates in database
        if db.is_configured():
            await db.create_or_update_user(
                platform=platform,
                user_id=user_id,
                metadata=session.metadata
            )
        return (True, "Please enter the title for the new context:")

    elif missing_context:
        session.metadata["tester_state"] = "awaiting_context"
        session.metadata["tester_draft"] = tester_draft
        
        # Persist session metadata updates in database
        if db.is_configured():
            await db.create_or_update_user(
                platform=platform,
                user_id=user_id,
                metadata=session.metadata
            )
        return (True, "Please enter the context rules/text:")

    # We have both title and context. Clear states and save!
    title = tester_draft["title"]
    context_text = tester_draft["context"]
    context_type = tester_draft.get("type", "universal")

    # Normalize type (defaults to universal)
    if context_type not in ("universal", "special"):
        context_type = "universal"

    # Clear tester state from metadata
    session.metadata.pop("tester_state", None)
    session.metadata.pop("tester_draft", None)
    
    if db.is_configured():
        await db.create_or_update_user(
            platform=platform,
            user_id=user_id,
            metadata=session.metadata
        )

    # Universal contexts are always active
    is_active = True if context_type == "universal" else False

    logger.info(
        f"🧪 Tester {user_name} ({platform}:{user_id}) creating context: "
        f"title='{title}', type={context_type}, is_active={is_active}"
    )

    try:
        await db.save_global_context(
            name=title,
            text=context_text,
            description=f"Created by tester {user_name} ({platform}:{user_id})",
            context_type=context_type,
            is_active=is_active,
        )

        # Re-compile guidelines.txt with the new context
        from main import compile_and_save_active_guidelines
        await compile_and_save_active_guidelines()

        logger.info(f"✅ Context '{title}' ({context_type}) created successfully by tester {user_name}")

        # Exact confirmation format:
        # "@confirmation
        # Context is added & thanks for making me better"
        confirmation = "@confirmation\nContext is added & thanks for making me better"
        return (True, confirmation)

    except Exception as e:
        logger.error(f"❌ Failed to save context from tester command: {e}")
        error_reply = (
            f"❌ Failed to add context '{title}'.\n"
            f"Error: {str(e)}\n\n"
            f"Please type @testing to try again."
        )
        return (True, error_reply)
