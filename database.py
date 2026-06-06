"""
Supabase Database Client Helper
==============================
Manages persistence for users, session history, leads, and orders.
Uses the official `supabase` client with async threadpools.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional
import time

from config import settings

logger = logging.getLogger(__name__)

# Lazy initialization of Supabase client to avoid crash if variables are not set
_supabase_client = None

def get_supabase_client():
    global _supabase_client
    if _supabase_client is not None:
        return _supabase_client

    if not settings.supabase_configured:
        logger.warning("⚠️ Supabase is not configured. Database persistence will be disabled.")
        return None

    try:
        from supabase import create_client, Client, ClientOptions
        import httpx
        options = ClientOptions(
            postgrest_client_timeout=15,
            httpx_client=httpx.Client(http2=False)
        )
        _supabase_client = create_client(settings.SUPABASE_URL, settings.SUPABASE_KEY, options=options)
        logger.info("✅ Supabase client initialized successfully (HTTP/2 disabled)")
        return _supabase_client
    except Exception as e:
        logger.error(f"❌ Failed to initialize Supabase client: {e}")
        return None


class SupabaseDB:
    """Helper class to interact with Supabase tables asynchronously."""

    def is_configured(self) -> bool:
        return get_supabase_client() is not None

    def _get_user_key(self, platform: str, user_id: str) -> str:
        return f"{platform}:{user_id}"

    async def get_user(self, platform: str, user_id: str) -> Optional[Dict[str, Any]]:
        """Fetch user profile by platform and user_id."""
        client = get_supabase_client()
        if not client:
            return None

        user_key = self._get_user_key(platform, user_id)
        try:
            # Run blocking database call in a separate thread
            def _query():
                res = client.table("users").select("*").eq("id", user_key).execute()
                return res.data

            data = await asyncio.to_thread(_query)
            if data and len(data) > 0:
                user = data[0]
                if "role" not in user or user["role"] is None:
                    user["role"] = user.get("metadata", {}).get("role", "Customer")
                return user
            return None
        except Exception as e:
            logger.error(f"Error querying user {user_key} from Supabase: {e}")
            return None

    async def create_or_update_user(
        self,
        platform: str,
        user_id: str,
        name: Optional[str] = None,
        phone: Optional[str] = None,
        address: Optional[str] = None,
        lead_type: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        human_handoff: Optional[bool] = None,
        role: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Upsert user profile."""
        client = get_supabase_client()
        user_key = self._get_user_key(platform, user_id)

        # Build updates payload
        data: Dict[str, Any] = {
            "id": user_key,
            "platform": platform,
            "user_id": user_id,
            "last_active": "now()",
        }
        if name is not None:
            data["name"] = name
        if phone is not None:
            data["phone"] = phone
        if address is not None:
            data["address"] = address
        if lead_type is not None:
            data["lead_type"] = lead_type
        if human_handoff is not None:
            data["human_handoff"] = human_handoff

        if not client:
            if role is not None:
                data["role"] = role
            return data

        try:
            if metadata is not None or role is not None:
                # Merge existing metadata if we can
                existing = await self.get_user(platform, user_id)
                existing_meta = existing.get("metadata", {}) if existing else {}
                meta_payload = metadata or {}
                if role is not None:
                    meta_payload["role"] = role
                merged_meta = {**existing_meta, **meta_payload}
                data["metadata"] = merged_meta

            # Try to upsert with role column first
            if role is not None:
                data["role"] = role

            def _upsert():
                res = client.table("users").upsert(data).execute()
                return res.data

            res_data = await asyncio.to_thread(_upsert)
            return res_data[0] if res_data else data
        except Exception as e:
            # If it failed and role was set, it might be because the role column doesn't exist in the schema.
            # Retry without the role column only if the error indicates a missing column/schema cache issue.
            is_column_missing = any(indicator in str(e) for indicator in ["PGRST204", "42703", "column", "role"])
            if role is not None and "role" in data and is_column_missing:
                logger.warning(f"Failed to upsert with role column (schema cache stale/column missing): {e}. Retrying using metadata fallback.")
                del data["role"]
                try:
                    def _upsert_fallback():
                        res = client.table("users").upsert(data).execute()
                        return res.data
                    res_data = await asyncio.to_thread(_upsert_fallback)
                    return res_data[0] if res_data else data
                except Exception as e2:
                    logger.error(f"Error in fallback upsert user {user_key}: {e2}")
                    return data
            else:
                logger.error(f"Error upserting user {user_key} in Supabase: {e}")
                return data

    async def increment_message_count(self, platform: str, user_id: str) -> None:
        """Increment user's message count by 1."""
        client = get_supabase_client()
        if not client:
            return

        user_key = self._get_user_key(platform, user_id)
        try:
            # Get current count
            existing = await self.get_user(platform, user_id)
            curr_count = existing.get("message_count", 0) if existing else 0

            await self.create_or_update_user(
                platform,
                user_id,
                lead_type=existing.get("lead_type") if existing else "cold",
            )

            # Simple updates increment
            def _update():
                client.table("users").update({
                    "message_count": curr_count + 1,
                    "last_active": "now()"
                }).eq("id", user_key).execute()

            await asyncio.to_thread(_update)
        except Exception as e:
            logger.error(f"Error incrementing message count for {user_key}: {e}")

    async def increment_order_count(self, platform: str, user_id: str) -> None:
        """Increment user's order count by 1."""
        client = get_supabase_client()
        if not client:
            return

        user_key = self._get_user_key(platform, user_id)
        try:
            existing = await self.get_user(platform, user_id)
            curr_count = existing.get("order_count", 0) if existing else 0

            def _update():
                client.table("users").update({
                    "order_count": curr_count + 1,
                    "last_active": "now()"
                }).eq("id", user_key).execute()

            await asyncio.to_thread(_update)
        except Exception as e:
            logger.error(f"Error incrementing order count for {user_key}: {e}")

    async def save_message(
        self,
        platform: str,
        user_id: str,
        role: str,
        content: Optional[str] = None,
        name: Optional[str] = None,
        tool_calls: Optional[List[Dict[str, Any]]] = None,
        tool_call_id: Optional[str] = None,
        message_id: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """Save a conversation message, avoiding duplicates if message_id is provided."""
        client = get_supabase_client()
        user_key = self._get_user_key(platform, user_id)

        # Ensure user exists first
        await self.create_or_update_user(platform, user_id)

        data = {
            "user_id": user_key,
            "role": role,
            "content": content,
            "name": name,
            "tool_calls": tool_calls,
            "tool_call_id": tool_call_id,
        }
        if message_id:
            data["message_id"] = message_id

        if not client:
            return None

        try:
            # If message_id is provided, check if it already exists to prevent duplicates
            if message_id:
                def _check():
                    res = client.table("messages").select("id").eq("message_id", message_id).execute()
                    return res.data
                existing = await asyncio.to_thread(_check)
                if existing:
                    return existing[0]

            def _insert():
                res = client.table("messages").insert(data).execute()
                return res.data

            res_data = await asyncio.to_thread(_insert)
            return res_data[0] if res_data else None
        except Exception as e:
            is_column_missing = any(indicator in str(e) for indicator in ["42703", "column", "message_id"])
            if message_id and "message_id" in data and is_column_missing:
                logger.warning(
                    f"Failed to save message with message_id (column missing): {e}. "
                    "Retrying fallback save without message_id."
                )
                del data["message_id"]
                try:
                    def _insert_fallback():
                        res = client.table("messages").insert(data).execute()
                        return res.data
                    res_data = await asyncio.to_thread(_insert_fallback)
                    return res_data[0] if res_data else None
                except Exception as e2:
                    logger.error(f"Error in fallback save message for {user_key}: {e2}")
                    return None
            else:
                logger.error(f"Error saving message for {user_key} to Supabase: {e}")
                return None


    async def check_and_register_message(
        self,
        platform: str,
        user_id: str,
        role: str,
        content: Optional[str] = None,
        message_id: Optional[str] = None,
        name: Optional[str] = None,
    ) -> bool:
        """
        Check if message_id already exists in Supabase.
        If it does, returns False (indicating it is a duplicate).
        If it does not, inserts the message immediately (acting as a lock) and returns True.
        """
        if not message_id:
            return True

        client = get_supabase_client()
        if not client:
            return True

        user_key = self._get_user_key(platform, user_id)

        # Ensure user exists first
        await self.create_or_update_user(platform, user_id)

        try:
            # Check if message_id exists
            def _check():
                res = client.table("messages").select("id").eq("message_id", message_id).execute()
                return res.data
            existing = await asyncio.to_thread(_check)
            if existing:
                return False

            # Insert the actual message as a lock
            data = {
                "user_id": user_key,
                "role": role,
                "content": content,
                "name": name,
                "message_id": message_id,
            }
            def _insert():
                client.table("messages").insert(data).execute()
            await asyncio.to_thread(_insert)
            return True
        except Exception as e:
            err_str = str(e)
            is_column_missing = any(indicator in err_str for indicator in ["42703", "column", "message_id"])
            if is_column_missing:
                logger.error(
                    "⚠️ DATABASE SCHEMA MISMATCH: The 'message_id' column is missing from your 'messages' table in Supabase. "
                    "This causes message saving to fail and disables duplicate reply prevention. "
                    "Please execute the following SQL in your Supabase SQL Editor to fix it:\n"
                    "    ALTER TABLE messages ADD COLUMN message_id TEXT UNIQUE;"
                )
                return True # Proceed to process the message even if we couldn't deduplicate

            logger.warning(f"Conflict or error registering message_id {message_id}: {e}")
            return False

    async def get_messages(self, platform: str, user_id: str, limit: int = 20) -> List[Dict[str, Any]]:
        """Retrieve recent message history sorted chronologically."""
        client = get_supabase_client()
        if not client:
            return []

        user_key = self._get_user_key(platform, user_id)
        try:
            def _query():
                # Query newest limit and reverse them to chronologically order
                res = (
                    client.table("messages")
                    .select("*")
                    .eq("user_id", user_key)
                    .order("created_at", desc=True)
                    .limit(limit)
                    .execute()
                )
                return res.data

            data = await asyncio.to_thread(_query)
            if data:
                # Reverse back so it is chronological (oldest first)
                data.reverse()
                # Clean up metadata fields to fit Chat API expected roles
                formatted = []
                for msg in data:
                    cleaned = {
                        "role": msg["role"],
                        "content": msg["content"],
                    }
                    if msg.get("name"):
                        cleaned["name"] = msg["name"]
                    if msg.get("tool_calls"):
                        cleaned["tool_calls"] = msg["tool_calls"]
                    if msg.get("tool_call_id"):
                        cleaned["tool_call_id"] = msg["tool_call_id"]
                    if msg.get("message_id"):
                        cleaned["message_id"] = msg["message_id"]
                    formatted.append(cleaned)
                return formatted
            return []
        except Exception as e:
            logger.error(f"Error loading messages for {user_key} from Supabase: {e}")
            return []

    async def save_context(
        self,
        platform: str,
        user_id: str,
        context_name: str,
        text: str,
    ) -> Optional[Dict[str, Any]]:
        """Upsert long-term user context (e.g. facts/preferences)."""
        client = get_supabase_client()
        user_key = self._get_user_key(platform, user_id)

        # Ensure user exists first
        await self.create_or_update_user(platform, user_id)

        data = {
            "user_id": user_key,
            "context_name": context_name,
            "text": text,
            "updated_at": "now()",
        }

        if not client:
            return data

        try:
            def _upsert():
                # On conflict of (user_id, context_name), update text and updated_at
                res = client.table("contexts").upsert(
                    data,
                    on_conflict="user_id,context_name"
                ).execute()
                return res.data

            res_data = await asyncio.to_thread(_upsert)
            return res_data[0] if res_data else data
        except Exception as e:
            logger.error(f"Error saving context {context_name} for {user_key} to Supabase: {e}")
            return data

    async def get_context(self, platform: str, user_id: str, context_name: str) -> Optional[str]:
        """Get context text for user."""
        client = get_supabase_client()
        if not client:
            return None

        user_key = self._get_user_key(platform, user_id)
        try:
            def _query():
                res = (
                    client.table("contexts")
                    .select("text")
                    .eq("user_id", user_key)
                    .eq("context_name", context_name)
                    .execute()
                )
                return res.data

            data = await asyncio.to_thread(_query)
            if data and len(data) > 0:
                return data[0]["text"]
            return None
        except Exception as e:
            logger.error(f"Error getting context {context_name} for {user_key}: {e}")
            return None

    async def get_global_contexts(self) -> List[Dict[str, Any]]:
        """Retrieve all global contexts (user_id is null) with local JSON fallback."""
        import json
        import os
        client = get_supabase_client()
        local_file = os.path.join(os.path.dirname(__file__), "global_contexts.json")

        db_contexts = []
        if client:
            try:
                def _query():
                    res = client.table("contexts").select("*").is_("user_id", "null").execute()
                    return res.data
                db_contexts = await asyncio.to_thread(_query) or []
            except Exception as e:
                logger.error(f"Error retrieving global contexts from Supabase: {e}")

        # Merge or fall back to local file if empty or DB offline
        if not client or not db_contexts:
            if os.path.exists(local_file):
                try:
                    with open(local_file, "r", encoding="utf-8") as f:
                        db_contexts = json.load(f)
                except Exception as e:
                    logger.error(f"Error reading local contexts: {e}")
                    db_contexts = []

        # Ensure all contexts have context_type and is_active properties
        for c in db_contexts:
            if c.get("context_name") == "Standard Sales Rules" or c.get("id") in ("c_default", "bac3b77b-8e91-5ff5-ac79-01b59613fa60", "4197e59c-6ba1-512c-96be-8ffc4155fb2f"):
                c["context_type"] = "universal"
                c["is_active"] = True

            if "context_type" not in c:
                c["context_type"] = "special"
            if "is_active" not in c:
                if c.get("context_type") == "universal":
                    c["is_active"] = True
                else:
                    c["is_active"] = False

        return db_contexts

    async def save_global_context(
        self,
        name: str,
        text: str,
        description: Optional[str] = None,
        context_id: Optional[str] = None,
        context_type: str = "special",
        is_active: bool = False
    ) -> Optional[Dict[str, Any]]:
        """Save or update a global context (user_id is null) with local JSON fallback."""
        import json
        import os
        import uuid
        client = get_supabase_client()
        local_file = os.path.join(os.path.dirname(__file__), "global_contexts.json")

        def _to_uuid(val: str) -> str:
            if not val:
                return str(uuid.uuid4())
            try:
                uuid.UUID(val)
                return val
            except ValueError:
                return str(uuid.uuid5(uuid.NAMESPACE_DNS, val))

        target_id = _to_uuid(context_id) if context_id else _to_uuid(name)

        # If type is universal, it must always be active
        if context_type == "universal":
            is_active = True

        # 1. Update/Write to local JSON file fallback if not running on Vercel
        import os
        if not os.getenv("VERCEL") or not client:
            try:
                contexts = []
                if os.path.exists(local_file):
                    with open(local_file, "r", encoding="utf-8") as f:
                        contexts = json.load(f)

                found = False
                for idx, c in enumerate(contexts):
                    c_id_cleaned = _to_uuid(c.get("id"))
                    if (context_id and c_id_cleaned == target_id) or (not context_id and c.get("context_name") == name):
                        target_id = c_id_cleaned or target_id
                        contexts[idx] = {
                            "id": target_id,
                            "context_name": name,
                            "description": description,
                            "text": text,
                            "context_type": context_type,
                            "is_active": is_active,
                            "user_id": None
                        }
                        found = True
                        break

                if not found:
                    contexts.append({
                        "id": target_id,
                        "context_name": name,
                        "description": description,
                        "text": text,
                        "context_type": context_type,
                        "is_active": is_active,
                        "user_id": None
                    })

                with open(local_file, "w", encoding="utf-8") as f:
                    json.dump(contexts, f, indent=4, ensure_ascii=False)
            except Exception as e:
                logger.warning(f"Failed to write context to local file: {e}")

        # 2. Update to Supabase if configured
        data = {
            "id": target_id,
            "context_name": name,
            "text": text,
            "user_id": None,
        }
        if description is not None:
            data["description"] = description

        fields_to_try = [
            {"context_type": context_type, "is_active": is_active},
            {"context_type": context_type},
            {}
        ]

        if not client:
            return data

        for extra_fields in fields_to_try:
            payload = {**data, **extra_fields}
            try:
                def _upsert():
                    res = client.table("contexts").upsert(payload).execute()
                    return res.data
                res_data = await asyncio.to_thread(_upsert)
                return res_data[0] if res_data else payload
            except Exception as e:
                logger.warning(f"Error saving global context with fields {extra_fields}: {e}. Retrying with fewer fields...")
                continue

        return data

    async def save_lead(
        self,
        platform: str,
        user_id: str,
        name: str,
        phone: str,
        interest: str,
    ) -> Optional[Dict[str, Any]]:
        """Save a new lead and update user profile."""
        client = get_supabase_client()
        user_key = self._get_user_key(platform, user_id)

        # 1. Update user profile details
        user = await self.get_user(platform, user_id)
        lead_type = "warm"
        if user and user.get("lead_type") in ("hot", "converted"):
            lead_type = user.get("lead_type")

        await self.create_or_update_user(
            platform,
            user_id,
            name=name,
            phone=phone,
            lead_type=lead_type,
        )

        data = {
            "user_id": user_key,
            "name": name,
            "phone": phone,
            "interest": interest,
            "platform": platform,
        }

        if not client:
            return data

        try:
            def _insert():
                res = client.table("leads").insert(data).execute()
                return res.data

            res_data = await asyncio.to_thread(_insert)
            return res_data[0] if res_data else data
        except Exception as e:
            logger.error(f"Error saving lead to Supabase: {e}")
            return data

    async def save_order(
        self,
        platform: str,
        user_id: str,
        name: str,
        phone: str,
        address: str,
        order_details: str,
    ) -> Optional[Dict[str, Any]]:
        """Save a confirmed order, increment order count, and update user profile."""
        client = get_supabase_client()
        user_key = self._get_user_key(platform, user_id)

        # 1. Update user details & status
        await self.create_or_update_user(
            platform,
            user_id,
            name=name,
            phone=phone,
            address=address,
            lead_type="converted",
        )
        # 2. Increment order count
        await self.increment_order_count(platform, user_id)

        data = {
            "user_id": user_key,
            "name": name,
            "phone": phone,
            "address": address,
            "order_details": order_details,
            "status": "pending",
        }

        if not client:
            return data

        try:
            def _insert():
                res = client.table("orders").insert(data).execute()
                return res.data

            res_data = await asyncio.to_thread(_insert)
            return res_data[0] if res_data else data
        except Exception as e:
            logger.error(f"Error saving order to Supabase: {e}")
            return data

    async def get_all_orders(self) -> List[Dict[str, Any]]:
        """Retrieve all orders from Supabase (fallback to empty list)."""
        client = get_supabase_client()
        if not client:
            return []

        try:
            def _query():
                res = client.table("orders").select("*").order("created_at", desc=True).execute()
                return res.data

            data = await asyncio.to_thread(_query)
            return data or []
        except Exception as e:
            logger.error(f"Error retrieving orders from Supabase: {e}")
            return []

    async def get_admin_users(self) -> List[Dict[str, Any]]:
        """Fetch all users who are Admins, checking both the role column and the metadata.role fallback."""
        client = get_supabase_client()
        if not client:
            return []
        try:
            def _query_column():
                res = client.table("users").select("*").eq("role", "Admin").execute()
                return res.data

            # Query where role column is 'Admin'
            data_col = await asyncio.to_thread(_query_column) or []

            # Since some admins might be stored in metadata fallback (as metadata: {"role": "Admin"}),
            # let's fetch all users to inspect metadata.
            def _query_all():
                res = client.table("users").select("*").execute()
                return res.data

            all_users = await asyncio.to_thread(_query_all) or []

            admins = {u["id"]: u for u in data_col}
            for u in all_users:
                user_role = u.get("role") or u.get("metadata", {}).get("role")
                if user_role == "Admin":
                    admins[u["id"]] = u
            return list(admins.values())
        except Exception as e:
            logger.error(f"Error querying admin users from Supabase: {e}")
            return []


    async def get_last_responded_msg_id(self, platform: str, user_id: str) -> Optional[str]:
        """Get the last responded message ID from Supabase (used for deduplication on Vercel)."""
        user_data = await self.get_user(platform, user_id)
        if user_data:
            return (user_data.get("metadata") or {}).get("last_responded_msg_id")
        return None

    async def set_last_responded_msg_id(self, platform: str, user_id: str, msg_id: str) -> None:
        """Persist last responded message ID to Supabase so it survives serverless restarts."""
        client = get_supabase_client()
        if not client:
            return
        user_key = self._get_user_key(platform, user_id)
        try:
            existing = await self.get_user(platform, user_id)
            existing_meta = (existing.get("metadata") or {}) if existing else {}
            existing_meta["last_responded_msg_id"] = msg_id
            def _update():
                client.table("users").update({"metadata": existing_meta}).eq("id", user_key).execute()
            await asyncio.to_thread(_update)
        except Exception as e:
            logger.error(f"Error setting last_responded_msg_id for {user_key}: {e}")


# Global singleton database instance
db = SupabaseDB()
