"""
AI Salesman Agent
=================
The brain of the chatbot. Uses tool calling via the Multi-AI Stable API
(Gemini → OpenAI → Groq with automatic failover) to answer customer
queries about products, orders, and store policies.

Flow:
  1. Receive user message
  2. Build messages array with system prompt + conversation history
  3. Call Multi-AI API with tool definitions
  4. If AI returns tool_calls, execute them and continue
  5. Return final text response to the user
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional, Tuple

import httpx

from config import settings
from agent.tool_executor import execute_tool

logger = logging.getLogger(__name__)


# ── System Prompt ────────────────────────────────────────────────────────

SYSTEM_PROMPT = f"""You are the AI Sales Assistant for {settings.STORE_NAME}, a trendy online fashion store based in Bangladesh.

🏪 Store: {settings.STORE_NAME}
🌐 Website: {settings.STORE_URL}
💰 Currency: {settings.STORE_CURRENCY} (Bangladeshi Taka, ৳)

## Your Personality
- Friendly, warm, and enthusiastic about fashion
- You speak naturally in both English and Bangla (বাংলা) — match the customer's language
- Use emojis sparingly to keep messages fun but professional
- Be helpful, concise, and proactive — suggest products, don't just answer questions

## Your Capabilities
You can help customers with:
1. **Product Search** — Find products by style, color, size, price range, brand, etc.
2. **Product Details** — Show sizes, colors, prices, stock availability for specific items
3. **Order Tracking** — Check order status by order ID number
4. **Store Info** — Answer questions about return policy, shipping, payment methods, etc.
5. **Lead Collection** — If a customer wants to be contacted, collect their name, phone, and interest
6. **Human Handoff** — Escalate complex issues to a human agent

## Important Rules
- ALWAYS use the search_products tool when a customer asks about products. Never guess product info.
- When showing products, include the price in ৳ (BDT) and the store link.
- If a customer mentions a price range like "under 1200", search accordingly and filter.
- For order tracking, ask for the order ID number if not provided.
- If you don't know something, say so honestly and offer to connect with a human.
- Never make up product information, prices, or stock availability.
- Keep responses concise — customers on Messenger/WhatsApp prefer short, quick messages.
- When showing multiple products, summarize them in a readable format with prices.
- If the customer seems frustrated or the issue is complex, offer human_handoff.

## Order Confirmation Flow
When a customer wants to place or confirm an order:
1. You MUST collect their Name, Phone Number, and Delivery/Shipping Address if they are missing from the "Customer Profile" section below.
2. You MUST confirm all product details (Product Name, Variant/Color, Size, Quantity, Price) with the customer.
3. If any required information is missing, ask the customer for it before calling the `confirm_order` tool.
4. Once all Name, Phone, Address, and product details are collected and confirmed, call the `confirm_order` tool. Do not guess any missing details.

## Response Format
- Keep messages under 500 characters when possible.
- NEVER use markdown formatting like asterisks (** or *), underscores (_), strikethroughs (~~), or headers (#) in your response. Write purely in clean plain text.
- Write like a real human in a chat: keep paragraphs short (1-3 sentences).
- If you need to send multiple distinct messages or ideas, separate them with a double newline (\n\n) so they can be sent as separate chat bubbles.
- For product lists, use a clean plain text format:
  1. Product Name — ৳Price
  2. Product Name — ৳Price
- Include the product URL: {settings.STORE_URL}/products/[slug]
"""


# ── Tool Definitions ─────────────────────────────────────────────────────

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "search_products",
            "description": "Search the fashion store product catalog. Use this whenever a customer asks about products, wants to browse, or mentions any clothing item, style, color, or price range.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Search query containing ONLY descriptive keywords (colors, styles, items) e.g., 'black oversized t-shirt', 'red dress', 'cotton shirt'. NEVER include prices, numbers, or terms like 'under', 'above', 'cheap' (e.g. use 'black t-shirt' instead of 'black t-shirt under 1200').",
                    },
                    "product_type": {
                        "type": "string",
                        "description": "Optional product category filter — e.g. 't-shirt', 'polo', 'hoodie', 'pant', 'shirt'",
                    },
                    "gender": {
                        "type": "string",
                        "description": "Optional gender filter — 'male', 'female', 'unisex'",
                    },
                    "brand_name": {
                        "type": "string",
                        "description": "Optional brand name filter",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Number of results to return (default 5, max 10)",
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_product_details",
            "description": "Get full details for a specific product including all variants (sizes, colors), stock availability, and description.",
            "parameters": {
                "type": "object",
                "properties": {
                    "product_id": {
                        "type": "integer",
                        "description": "The product ID number",
                    },
                    "slug": {
                        "type": "string",
                        "description": "The product URL slug",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "check_order_status",
            "description": "Check the current status of a customer's order. Ask the customer for their order ID number.",
            "parameters": {
                "type": "object",
                "properties": {
                    "order_id": {
                        "type": "integer",
                        "description": "The order ID number (e.g. 12345)",
                    },
                },
                "required": ["order_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_store_info",
            "description": "Get store policies and FAQs. Topics: return_policy, shipping, payment, contact, general",
            "parameters": {
                "type": "object",
                "properties": {
                    "topic": {
                        "type": "string",
                        "enum": ["return_policy", "shipping", "payment", "contact", "general"],
                        "description": "The topic to get information about",
                    },
                },
                "required": ["topic"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "collect_lead",
            "description": "Save customer contact info for human follow-up. Use when a customer wants to be called back or wants personalized help.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Customer's name",
                    },
                    "phone": {
                        "type": "string",
                        "description": "Customer's phone number",
                    },
                    "interest": {
                        "type": "string",
                        "description": "What the customer is interested in",
                    },
                },
                "required": ["name", "phone", "interest"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "request_human_agent",
            "description": "Escalate to a human agent when the AI cannot handle the request or the customer explicitly asks for a human.",
            "parameters": {
                "type": "object",
                "properties": {
                    "reason": {
                        "type": "string",
                        "description": "Reason for escalation",
                    },
                },
                "required": ["reason"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "confirm_order",
            "description": "Confirm a customer order once all required details are collected (customer's name, phone, address) and product details are confirmed.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Customer's full name",
                    },
                    "phone": {
                        "type": "string",
                        "description": "Customer's phone number",
                    },
                    "address": {
                        "type": "string",
                        "description": "Customer's full shipping/delivery address",
                    },
                    "product_name": {
                        "type": "string",
                        "description": "The name of the product being ordered",
                    },
                    "variant": {
                        "type": "string",
                        "description": "The variant/color of the product (e.g. 'Royal Blue', 'Black')",
                    },
                    "size": {
                        "type": "string",
                        "description": "The size of the product (e.g. 'M', 'L', 'XL')",
                    },
                    "quantity": {
                        "type": "integer",
                        "description": "The quantity being ordered",
                    },
                    "unit_price": {
                        "type": "number",
                        "description": "The unit price of the product in BDT",
                    },
                    "total_price": {
                        "type": "number",
                        "description": "The total price of the order (unit_price * quantity) in BDT",
                    },
                    "customer_comment": {
                        "type": "string",
                        "description": "Any comments or special instructions from the customer (optional)",
                    },
                },
                "required": [
                    "name",
                    "phone",
                    "address",
                    "product_name",
                    "variant",
                    "size",
                    "quantity",
                    "unit_price",
                    "total_price",
                ],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_customer_profile",
            "description": "Update details about the customer such as their name, phone number, or shipping address when they share them in conversation.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Customer's name if provided",
                    },
                    "phone": {
                        "type": "string",
                        "description": "Customer's phone number if provided",
                    },
                    "address": {
                        "type": "string",
                        "description": "Customer's shipping/delivery address if provided",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "save_customer_fact",
            "description": "Save or update a specific fact, preference, or detail about the customer (e.g., size preference, style interests, color preferences, budget, or family members) to remember it for future conversation.",
            "parameters": {
                "type": "object",
                "properties": {
                    "fact": {
                        "type": "string",
                        "description": "The key detail or fact to remember. Write as a concise bullet point or short description, e.g. 'Prefers black oversized t-shirts, size XL.' or 'Buying a gift for their brother.'",
                    },
                },
                "required": ["fact"],
            },
        },
    },
]


# ── AI Agent ─────────────────────────────────────────────────────────────

MAX_TOOL_ROUNDS = 3  # Prevent infinite tool-call loops


async def get_ai_response(
    messages: List[Dict],
    platform: str = "messenger",
    user_id: str = "",
) -> Tuple[str, List[Dict]]:
    """
    Get an AI response with tool calling support.

    Returns:
        (response_text, updated_messages)
    """
    # Retrieve user profile & facts from database
    user_context_str = ""
    try:
        from database import db
        if db.is_configured():
            user_data = await db.get_user(platform, user_id)
            if user_data:
                user_context_str += f"\n\n## Customer Profile (Information you know about this customer):\n"
                user_context_str += f"- Platform: {platform}\n"
                user_context_str += f"- User ID: {user_id}\n"
                if user_data.get("name"):
                    user_context_str += f"- Name: {user_data.get('name')}\n"
                if user_data.get("phone"):
                    user_context_str += f"- Phone Number: {user_data.get('phone')}\n"
                if user_data.get("address"):
                    user_context_str += f"- Delivery/Shipping Address: {user_data.get('address')}\n"
                user_context_str += f"- Lead Status: {user_data.get('lead_type', 'cold')}\n"
                user_context_str += f"- Order Count: {user_data.get('order_count', 0)}\n"
                user_context_str += f"- Message Count: {user_data.get('message_count', 0)}\n"

            facts = await db.get_context(platform, user_id, "user_facts")
            if facts:
                user_context_str += f"\n## Saved Customer Preferences & Facts:\n{facts}\n"
    except Exception as e:
        logger.error(f"Error fetching customer context from Supabase: {e}")

    # Load dynamic guidelines
    system_prompt = SYSTEM_PROMPT
    if user_context_str:
        system_prompt += user_context_str

    # Load dynamic guidelines directly from database contexts
    try:
        from database import db
        contexts = await db.get_global_contexts()
        compiled_parts = []
        
        # Sort contexts: universal first, then special so they append in a consistent, logical order
        sorted_contexts = sorted(
            contexts, 
            key=lambda c: 0 if c.get("context_type") == "universal" else 1
        )
        
        for c in sorted_contexts:
            c_type = c.get("context_type", "special")
            is_act = c.get("is_active", False)
            
            # Universal context is always active, special is active only if checked
            if c_type == "universal" or is_act:
                header = f"# CONTEXT: {c.get('context_name')} ({c_type.upper()})"
                text_body = c.get("text", "").strip()
                if text_body:
                    compiled_parts.append(f"{header}\n\n{text_body}")
                    
        if compiled_parts:
            custom_guidelines = "\n\n# ======================================================\n\n".join(compiled_parts)
            system_prompt += f"\n\n## Custom Guidelines / Context (Adhere to this strictly):\n{custom_guidelines}"
            logger.info(f"✅ Loaded active guidelines directly from database ({len(custom_guidelines)} chars)")
    except Exception as e:
        logger.error(f"Error loading custom guidelines: {e}")

    # Inject active mode instructions dynamically
    try:
        from conversation.manager import conversation_manager
        session = await conversation_manager.get_or_create(platform, user_id)
        current_mode = session.metadata.get("current_mode")
        if current_mode:
            logger.info(f"📚 Session active mode detected: {current_mode}")
            from database import db
            db_commands = await db.get_command_modes()
            
            if current_mode == "documentation":
                # List commands relevant to Admin or Tester
                doc_lines = []
                for cmd in db_commands:
                    cmd_trigger = cmd.get("command")
                    cmd_user = ", ".join(cmd.get("command_user", []))
                    comments = cmd.get("comments", "")
                    doc_lines.append(f"- `@{cmd_trigger}` (Allowed roles: {cmd_user}): {comments}")
                
                commands_doc = "\n".join(doc_lines)
                
                documentation_instruction = (
                    f"\n\n## ACTIVE MODE: DOCUMENTATION MODE\n"
                    f"You are currently in documentation mode. The customer has requested to see the documentation of all commands they can use.\n"
                    f"You MUST formulate a clean plain text message listing all available commands and their descriptions.\n"
                    f"Here are the available commands from the database:\n"
                    f"{commands_doc}\n\n"
                    f"CRITICAL RULES:\n"
                    f"1. You must output the termination tag `@doc_response` followed by a newline (\\n) and then your documentation text (e.g., \"@doc_response\\nHere is the list of commands:\").\n"
                    f"2. Write your response in clean plain text without markdown formatting symbols."
                )
                system_prompt += documentation_instruction

            elif current_mode == "testing":
                testing_instruction = (
                    f"\n\n## ACTIVE MODE: TESTING MODE\n"
                    f"You are currently in testing mode. The customer (Admin/Tester) may send a message containing `@end` to request terminating testing mode.\n"
                    f"If the customer requested to end/terminate testing mode (e.g., they sent `@end`), you MUST acknowledge this and output the termination command `@test_terminate` followed by a newline (\\n) and then your confirmation text (e.g., \"@test_terminate\\nTesting mode has been terminated.\").\n"
                    f"Write your response in clean plain text without markdown formatting symbols."
                )
                system_prompt += testing_instruction

            elif current_mode == "setdomain":
                setdomain_instruction = (
                    f"\n\n## ACTIVE MODE: SETDOMAIN MODE\n"
                    f"You are currently in setdomain mode. The customer (Admin) is configuring the global reply domain filter (1=Admin, 2=Admin+Tester, 3=All).\n"
                    f"If the setting was completed successfully (e.g. they provided a valid value 1, 2, or 3), you MUST output the termination command `@domaindone` followed by a newline (\\n) and then confirmation text (e.g., \"@domaindone\\nDomain setting has been updated.\").\n"
                    f"Write your response in clean plain text without markdown formatting symbols."
                )
                system_prompt += setdomain_instruction
    except Exception as e:
        logger.error(f"Error checking active mode for system prompt: {e}")

    # Build initial messages with system prompt
    full_messages = [{"role": "system", "content": system_prompt}] + messages

    for round_num in range(MAX_TOOL_ROUNDS):
        logger.info(f"AI round {round_num + 1} for {platform}:{user_id}")

        # Call the AI with tools
        ai_result = await _call_ai_with_tools(full_messages)

        if "error" in ai_result:
            logger.error(f"AI error: {ai_result['error']}")
            return (
                "I'm sorry, I'm having trouble right now. Please try again or visit our store at "
                + settings.STORE_URL,
                messages,
            )

        text = ai_result.get("text", "")
        tool_calls = ai_result.get("tool_calls", [])

        if not tool_calls:
            # AI gave a final text response — we're done
            return text, messages

        # Execute each tool call
        logger.info(f"AI requested {len(tool_calls)} tool call(s)")

        # Add assistant message with tool calls to history
        assistant_msg: Dict[str, Any] = {"role": "assistant", "content": text or None}
        assistant_msg["tool_calls"] = tool_calls
        full_messages.append(assistant_msg)

        for tc in tool_calls:
            tool_name = tc.get("name", "")
            tool_id = tc.get("id", tool_name)

            # Parse arguments
            raw_args = tc.get("arguments", {})
            if isinstance(raw_args, str):
                try:
                    raw_args = json.loads(raw_args)
                except json.JSONDecodeError:
                    raw_args = {}

            # Add platform/user_id context to specific tools
            if tool_name in ("collect_lead", "confirm_order", "update_customer_profile", "save_customer_fact"):
                raw_args["platform"] = platform
                raw_args["user_id"] = user_id

            logger.info(f"Executing tool: {tool_name}({raw_args})")
            result = await execute_tool(tool_name, raw_args)

            # Add tool result to messages
            full_messages.append({
                "role": "tool",
                "content": result,
                "tool_call_id": tool_id,
                "name": tool_name,
            })


    # If we exhausted all rounds, return whatever text we have
    return text or "I found some information. How else can I help you?", messages


provider_cooldowns: Dict[str, float] = {}

def mark_provider_failed(provider: str) -> None:
    import time
    logger.warning(f"⚠️ Provider '{provider}' marked as failed. Putting on cooldown for 5 minutes.")
    provider_cooldowns[provider] = time.time() + 300

def is_provider_on_cooldown(provider: str) -> bool:
    import time
    cooldown_until = provider_cooldowns.get(provider, 0.0)
    return time.time() < cooldown_until


async def _call_ai_with_tools(messages: List[Dict]) -> Dict:
    """
    Call the AI providers with failover and cooldown support,
    defaulting to the primary provider if configured.
    """
    primary = settings.PRIMARY_AI_PROVIDER.lower().strip() if hasattr(settings, "PRIMARY_AI_PROVIDER") else "gemini"
    default_order = ["openai", "gemini", "groq", "multi-ai"]
    if primary in default_order:
        order = [primary] + [p for p in default_order if p != primary]
    else:
        order = default_order

    for provider in order:
        # Check if configured
        configured = False
        if provider == "openai":
            configured = bool(settings.OPENAI_API_KEY)
        elif provider == "gemini":
            configured = bool(settings.GEMINI_API_KEY)
        elif provider == "groq":
            configured = bool(settings.GROQ_API_KEY)
        elif provider == "multi-ai":
            configured = bool(settings.MULTI_AI_API_URL)

        if not configured:
            continue

        if is_provider_on_cooldown(provider):
            logger.info(f"⏭️ Skipping provider '{provider}' because it is on cooldown.")
            continue

        try:
            logger.info(f"🤖 Calling {provider} API directly...")
            if provider == "openai":
                result = await _call_openai_direct(messages)
            elif provider == "gemini":
                result = await _call_gemini_direct(messages)
            elif provider == "groq":
                result = await _call_groq_direct(messages)
            elif provider == "multi-ai":
                result = await _call_multi_ai(messages)
            else:
                continue

            if "error" not in result:
                return result

            # If it returned an error, mark as failed
            logger.warning(f"{provider} direct error: {result.get('error')}")
            mark_provider_failed(provider)
        except Exception as e:
            logger.error(f"{provider} direct call failed: {e}")
            mark_provider_failed(provider)

    return {"error": "No AI provider available"}


async def _call_multi_ai(messages: List[Dict]) -> Dict:
    """Call the Multi-AI Stable API /api/chat/tools endpoint."""
    url = f"{settings.MULTI_AI_API_URL}/api/chat/tools"

    # Convert messages to the format expected by Multi-AI API
    api_messages = []
    for msg in messages:
        m: Dict[str, Any] = {"role": msg["role"]}
        if msg.get("content") is not None:
            m["content"] = msg["content"]
        if msg.get("tool_calls"):
            m["tool_calls"] = msg["tool_calls"]
        if msg.get("tool_call_id"):
            m["tool_call_id"] = msg["tool_call_id"]
        if msg.get("name"):
            m["name"] = msg["name"]
        api_messages.append(m)

    payload = {
        "messages": api_messages,
        "tools": TOOLS,
        "options": {"temperature": 0.7, "max_tokens": 1024},
    }

    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.post(url, json=payload)

    if response.status_code != 200:
        return {"error": f"Multi-AI API returned {response.status_code}: {response.text}"}

    data = response.json()
    return {
        "text": data.get("text", ""),
        "tool_calls": data.get("tool_calls", []),
        "provider": data.get("provider", "unknown"),
    }


async def _call_gemini_direct(messages: List[Dict]) -> Dict:
    """
    Fallback: call Gemini API directly with function calling.
    Uses the google-generativeai REST API.
    """
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent"
    params = {"key": settings.GEMINI_API_KEY}

    # Convert to Gemini format
    contents = []
    system_instruction = None

    for msg in messages:
        role = msg["role"]

        if role == "system":
            system_instruction = {"parts": [{"text": msg["content"]}]}
            continue

        if role == "assistant":
            gemini_role = "model"
        elif role == "tool":
            # Gemini uses functionResponse
            contents.append({
                "role": "function",
                "parts": [{
                    "functionResponse": {
                        "name": msg.get("name", ""),
                        "response": {
                            "content": json.loads(msg["content"]) if isinstance(msg["content"], str) else msg["content"],
                        },
                    }
                }],
            })
            continue
        else:
            gemini_role = "user"

        parts = []
        if msg.get("content"):
            parts.append({"text": msg["content"]})

        # Handle tool calls from assistant
        if role == "assistant" and msg.get("tool_calls"):
            for tc in msg["tool_calls"]:
                raw_args = tc.get("arguments", {})
                if isinstance(raw_args, str):
                    try:
                        raw_args = json.loads(raw_args)
                    except json.JSONDecodeError:
                        raw_args = {}

                parts.append({
                    "functionCall": {
                        "name": tc.get("name", ""),
                        "args": raw_args,
                    }
                })

        if parts:
            contents.append({"role": gemini_role, "parts": parts})

    # Convert tools to Gemini format
    gemini_tools = [{
        "functionDeclarations": [
            {
                "name": t["function"]["name"],
                "description": t["function"]["description"],
                "parameters": t["function"].get("parameters", {}),
            }
            for t in TOOLS
        ]
    }]

    body: Dict[str, Any] = {
        "contents": contents,
        "tools": gemini_tools,
        "generationConfig": {
            "temperature": 0.7,
            "maxOutputTokens": 1024,
        },
    }
    if system_instruction:
        body["systemInstruction"] = system_instruction

    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.post(url, params=params, json=body)

    if response.status_code != 200:
        return {"error": f"Gemini API returned {response.status_code}: {response.text}"}

    data = response.json()

    # Parse Gemini response
    candidates = data.get("candidates", [])
    if not candidates:
        return {"error": "No candidates in Gemini response"}

    content = candidates[0].get("content", {})
    parts = content.get("parts", [])

    text = ""
    tool_calls = []

    for part in parts:
        if "text" in part:
            text += part["text"]
        elif "functionCall" in part:
            fc = part["functionCall"]
            tool_calls.append({
                "id": f"call_{fc['name']}",
                "name": fc["name"],
                "arguments": fc.get("args", {}),
            })

    return {
        "text": text,
        "tool_calls": tool_calls,
        "provider": "gemini-direct",
    }


async def _call_openai_direct(messages: List[Dict]) -> Dict:
    """Call OpenAI API directly."""
    return await _call_openai_compatible_direct(
        messages=messages,
        api_key=settings.OPENAI_API_KEY,
        base_url="https://api.openai.com/v1",
        model="gpt-4o-mini"
    )


async def _call_groq_direct(messages: List[Dict]) -> Dict:
    """Call Groq API directly."""
    return await _call_openai_compatible_direct(
        messages=messages,
        api_key=settings.GROQ_API_KEY,
        base_url="https://api.groq.com/openai/v1",
        model="llama-3.3-70b-versatile"
    )


async def _call_openai_compatible_direct(
    messages: List[Dict], api_key: str, base_url: str, model: str
) -> Dict:
    """Call an OpenAI-compatible API (like OpenAI or Groq) with function calling."""
    formatted_messages = []
    for msg in messages:
        m = {"role": msg["role"]}
        if msg.get("content") is not None:
            m["content"] = msg["content"]
        if msg.get("tool_calls"):
            m["tool_calls"] = [
                {
                    "id": tc.get("id"),
                    "type": "function",
                    "function": {
                        "name": tc.get("name"),
                        "arguments": json.dumps(tc.get("arguments")) if isinstance(tc.get("arguments"), dict) else tc.get("arguments")
                    }
                }
                for tc in msg["tool_calls"]
            ]
        if msg.get("tool_call_id"):
            m["tool_call_id"] = msg["tool_call_id"]
        if msg.get("name"):
            m["name"] = msg["name"]
        formatted_messages.append(m)

    preview_msgs = []
    for m in formatted_messages:
        content = m.get("content") or ""
        preview_content = content[:150] + "..." if len(content) > 150 else content
        preview_msgs.append({
            "role": m["role"],
            "content": preview_content,
            "tool_calls": len(m.get("tool_calls", [])) if m.get("tool_calls") else 0,
            "tool_call_id": m.get("tool_call_id")
        })
    logger.info(f"📤 OpenAI API call messages preview: {preview_msgs}")

    payload = {
        "model": model,
        "messages": formatted_messages,
        "tools": TOOLS,
        "tool_choice": "auto",
        "temperature": 0.7,
        "max_tokens": 1024,
    }
    
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }
    
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(f"{base_url}/chat/completions", headers=headers, json=payload)
            
        if response.status_code != 200:
            return {"error": f"OpenAI-compatible API returned {response.status_code}: {response.text}"}
            
        data = response.json()
        choices = data.get("choices", [])
        if not choices:
            return {"error": "No choices returned by API"}
            
        message = choices[0].get("message", {})
        text = message.get("content") or ""
        
        tool_calls = []
        raw_tool_calls = message.get("tool_calls", [])
        for tc in raw_tool_calls:
            func = tc.get("function", {})
            args = func.get("arguments", "{}")
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    args = {}
            tool_calls.append({
                "id": tc.get("id"),
                "name": func.get("name"),
                "arguments": args
            })
            
        return {
            "text": text,
            "tool_calls": tool_calls,
            "provider": f"{model}-direct"
        }
    except Exception as e:
        return {"error": f"Exception calling OpenAI-compatible API: {e}"}
