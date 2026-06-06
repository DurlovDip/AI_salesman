"""
Dynamic Command Processor
=========================
Loads allowed command modes from the database and processes them dynamically.
Supports Human-initiated commands (inputs) and AI-initiated commands (outputs).
"""

from __future__ import annotations

import logging
import re
import time
from typing import Optional, Tuple, Dict, Any, List

from database import db
from conversation.manager import conversation_manager

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


async def find_matching_commands(text: str, responder: str = "Human") -> List[Tuple[Dict[str, Any], int, int, str]]:
    """
    Find all words starting with @ in text and match them fuzzy/exactly to DB commands.
    Returns a list of tuples: (command_record, start_pos, end_pos, command_word)
    sorted by start_pos.
    """
    if not text:
        return []
        
    db_commands = await db.get_command_modes()
    if not db_commands:
        return []
        
    # Filter by responder (Human or AI)
    db_commands = [c for c in db_commands if c.get("responder", "").lower() == responder.lower()]
    
    matches = []
    # Find all words starting with @ preceded by space or start of line
    for m in re.finditer(r'(?:^|\s)(@\w+)', text):
        word = m.group(1)
        word_clean = word[1:].lower()  # strip @
        
        best_match = None
        best_dist = 999
        
        for cmd in db_commands:
            cmd_trigger = cmd.get("command", "").lower()
            # If word is very short, exact match or distance 1
            max_dist = 2 if len(cmd_trigger) > 4 else 1
            if word_clean == cmd_trigger:
                best_match = cmd
                best_dist = 0
                break
            
            dist = levenshtein_distance(word_clean, cmd_trigger)
            if dist <= max_dist and dist < best_dist:
                best_match = cmd
                best_dist = dist
                
        if best_match:
            start = m.start(1)
            end = m.end(1)
            matches.append((best_match, start, end, word))
            
    # Sort by start position
    matches.sort(key=lambda x: x[1])
    return matches


def extract_command_arguments(text: str, matches: List[Tuple[Dict[str, Any], int, int, str]]) -> Dict[str, str]:
    """
    For each matched command, extract the text after it up to the next command or end of string.
    """
    args = {}
    for i, (cmd, start, end, word) in enumerate(matches):
        cmd_name = cmd["command"]
        content_start = end
        content_end = matches[i + 1][1] if i + 1 < len(matches) else len(text)
        content = text[content_start:content_end].strip()
        
        # Clean up leading separator characters like -, :, =
        content = re.sub(r'^[:\-\s=]+', '', content).strip()
        args[cmd_name] = content
    return args


async def handle_tester_command(
    platform: str,
    user_id: str,
    user_text: str,
    user_name: str = "Tester",
) -> Tuple[bool, Optional[str]]:
    """
    Intercepts messages containing Human commands from authorized users
    and manages the step-by-step state machine flows.
    """
    # 1. Fetch user data to check role
    user_data = await db.get_user(platform, user_id)
    user_role = "Customer"
    if user_data:
        user_role = user_data.get("role") or user_data.get("metadata", {}).get("role", "Customer")

    # 2. Get active command modes from database
    db_commands = await db.get_command_modes()
    if not db_commands:
        return False, None

    # 3. Find any human commands in user message
    matches = await find_matching_commands(user_text, responder="Human")
    
    # Check if we are currently in an active mode for this user
    session = await conversation_manager.get_or_create(platform, user_id, user_name)
    current_mode = session.metadata.get("current_mode")
    
    # If no commands matched and we are not in an active mode, pass to AI
    if not matches and not current_mode:
        return False, None

    # Verify authorization for matched commands
    if matches:
        for cmd, _, _, _ in matches:
            allowed_users = [u.lower() for u in cmd.get("command_user", [])]
            if user_role.lower() not in allowed_users:
                logger.warning(f"User {user_id} with role {user_role} is unauthorized to run command @{cmd['command']}")
                return True, f"⚠️ You are not authorized to use the @{cmd['command']} command."

    # Parse arguments
    args = extract_command_arguments(user_text, matches)
    
    # Let's check if there's an initiator command
    initiator_match = next((m for m in matches if m[0]["command_type"] == "initiator"), None)
    
    if initiator_match:
        # Start the mode!
        cmd = initiator_match[0]
        mode_name = cmd["mode"]
        current_mode = mode_name
        session.metadata["current_mode"] = mode_name
        session.metadata["mode_state"] = {"draft": {}, "awaiting": None}
        
    # Process mediator commands
    if current_mode:
        # Update drafts with any mediator command values in this message
        for cmd, _, _, _ in matches:
            cmd_name = cmd["command"]
            if cmd["command_type"] == "mediator" and cmd["mode"] == current_mode:
                # Special case: @end command is sent to AI so AI can output @test_terminate
                if cmd_name == "end":
                    # Clear current_mode locally so AI receives the message, but do not terminate session yet.
                    return False, None
                
                session.metadata["mode_state"]["draft"][cmd_name] = args.get(cmd_name, "")
                session.metadata["mode_state"]["awaiting"] = None
                
        # If no commands matched, but we are in a mode and awaiting input, assign text to draft
        if not matches and session.metadata["mode_state"].get("awaiting"):
            awaiting = session.metadata["mode_state"]["awaiting"]
            clean_text = clean_plain_input(user_text, f"@{awaiting}")
            session.metadata["mode_state"]["draft"][awaiting] = clean_text
            session.metadata["mode_state"]["awaiting"] = None

        # Manage the step-by-step state machine flows based on mode
        if current_mode == "testing":
            draft = session.metadata["mode_state"]["draft"]
            
            # Check for missing parameters
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

            # We have both title and context. Save it!
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


async def handle_ai_response_commands(platform: str, user_id: str, text: str) -> str:
    """
    Parses the AI response text for commands (responder = 'AI').
    Executes the command side-effects and strips command tokens from the text.
    """
    if not text:
        return text

    # Find matches in AI output
    matches = await find_matching_commands(text, responder="AI")
    if not matches:
        return text

    cleaned_text = text
    session = await conversation_manager.get_or_create(platform, user_id)

    for cmd, start, end, word in matches:
        cmd_name = cmd["command"]
        cmd_type = cmd["command_type"]
        
        logger.info(f"🤖 Intercepted AI command: {word} (type: {cmd_type})")

        # 1. Execute actions/side-effects
        if cmd_name == "notification":
            # Send notification to admins
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
                        # Send notification to admin
                        from agent.tool_executor import _send_platform_text
                        await _send_platform_text(adm_plat, adm_uid, notification_msg)
            except Exception as e:
                logger.error(f"Failed to send admin notification: {e}")

        elif cmd_name == "test_terminate":
            # Terminate testing mode
            session.metadata.pop("current_mode", None)
            session.metadata.pop("mode_state", None)
            if db.is_configured():
                await db.create_or_update_user(platform=platform, user_id=user_id, metadata=session.metadata)
            logger.info("🧪 Testing mode terminated by AI.")

        # 2. Strip the command token from the final output text
        cleaned_text = cleaned_text.replace(word, "").strip()
        cleaned_text = re.sub(r'\s+', ' ', cleaned_text).strip()

    return cleaned_text
