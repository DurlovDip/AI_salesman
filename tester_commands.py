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
    
    # Check strict format for matches (newline is mandatory after command name if followed by arguments)
    if matches:
        for cmd, start, end, word in matches:
            idx = matches.index((cmd, start, end, word))
            next_start = matches[idx + 1][1] if idx + 1 < len(matches) else len(user_text)
            trailing_text = user_text[end:next_start]
            if trailing_text.strip():
                # Extract characters between matched word and the first non-whitespace character in trailing_text
                separator = trailing_text[:len(trailing_text) - len(trailing_text.lstrip())]
                if "\n" not in separator:
                    logger.warning(f"Command @{cmd['command']} failed strict newline structure validation.")
                    return True, "Invalid command structure. Newline is mandatory after the command name."

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
                return True, f"You are not authorized to use the @{cmd['command']} command."

    # Parse arguments
    args = extract_command_arguments(user_text, matches)
    
    # Resolve handler using the modular commands registry
    from commands.registry import get_handler

    if matches:
        # Sort matches so initiator commands run first to initialize the state correctly
        sorted_matches = sorted(matches, key=lambda m: 0 if m[0].get("command_type") == "initiator" else 1)
        
        last_handled = False
        last_reply = None

        for cmd_record, start, end, word in sorted_matches:
            cmd_name = cmd_record["command"]
            handler = get_handler(cmd_name)
            if handler:
                handled, reply = await handler.execute(
                    session=session,
                    platform=platform,
                    user_id=user_id,
                    command_record=cmd_record,
                    arguments=args,
                    user_text=user_text,
                    user_name=user_name,
                    user_role=user_role
                )
                if handled:
                    last_handled = True
                    if reply is not None:
                        last_reply = reply

        if last_handled:
            return last_handled, last_reply

    # Handle plain response text if we are in an active mode and awaiting a parameter
    elif current_mode:
        mode_cmds = [c for c in db_commands if c.get("mode") == current_mode]
        if mode_cmds:
            # Map active mode to the handler of its first command record
            handler = get_handler(mode_cmds[0]["command"])
            if handler:
                mock_record = {
                    "command": None,
                    "command_type": None,
                }
                handled, reply = await handler.execute(
                    session=session,
                    platform=platform,
                    user_id=user_id,
                    command_record=mock_record,
                    arguments={},
                    user_text=user_text,
                    user_name=user_name,
                    user_role=user_role
                )
                return handled, reply

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

    from commands.registry import get_handler

    for cmd, start, end, word in matches:
        cmd_name = cmd["command"]
        logger.info(f"🤖 Intercepted AI command: {word} (type: {cmd.get('command_type')})")

        # 1. Resolve and execute command handler dynamically
        handler = get_handler(cmd_name)
        if handler:
            await handler.execute(
                session=session,
                platform=platform,
                user_id=user_id,
                command_record=cmd,
                arguments={},
                user_text=text,
                user_name="AI",
                user_role="AI"
            )

        # 2. Strip the command token and its trailing newline/whitespace from the final output text
        pattern = re.escape(word) + r'\s*'
        cleaned_text = re.sub(pattern, '', cleaned_text, count=1).strip()
        cleaned_text = re.sub(r'[ \t]+', ' ', cleaned_text).strip()

    return cleaned_text
