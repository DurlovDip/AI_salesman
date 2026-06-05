"""
Tool Executor
=============
Executes AI tool calls against the Fashion ARC backend API.
Each function corresponds to a tool the AI agent can invoke.
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, Dict, List, Optional

import httpx

from config import settings

logger = logging.getLogger(__name__)


# ── HTTP Client Helper ───────────────────────────────────────────────────


async def _api_get(path: str, params: Optional[Dict] = None) -> Dict:
    """Make a GET request to the Fashion ARC backend."""
    url = f"{settings.FASHION_ARC_API_URL}{path}"
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.get(url, params=params)
        if response.status_code == 200:
            return response.json()
        else:
            logger.warning(f"API {response.status_code}: {url}")
            return {"error": f"API returned {response.status_code}"}
    except Exception as e:
        logger.error(f"API error for {url}: {e}")
        return {"error": str(e)}


# ── Tool Functions ───────────────────────────────────────────────────────


async def search_products(
    query: str,
    product_type: Optional[str] = None,
    gender: Optional[str] = None,
    brand_name: Optional[str] = None,
    subcategory: Optional[str] = None,
    limit: int = 5,
) -> str:
    """
    Search the Fashion ARC product catalog using the smart search engine.
    Returns JSON string of matching products.
    """
    params: Dict[str, Any] = {"q": query, "limit": min(limit, 10)}
    if product_type:
        # Normalize product type (e.g. "t-shirt" -> "tshirt")
        norm_type = product_type.lower().strip().replace("-", "")
        params["product_type"] = norm_type
    if gender:
        params["gender"] = gender
    if brand_name:
        params["brand_name"] = brand_name
    if subcategory:
        params["subcategory"] = subcategory

    data = await _api_get("/products/smart-search", params)

    if "error" in data:
        return json.dumps({"error": data["error"], "products": []})

    # Extract and simplify product data for the AI
    items = data.get("items", [])
    simplified = []
    for item in items[:limit]:
        simplified.append({
            "id": item.get("id"),
            "title": item.get("title"),
            "slug": item.get("slug"),
            "price": item.get("base_price") or item.get("product_price_taka"),
            "brand": item.get("brand_name"),
            "type": item.get("product_type"),
            "gender": item.get("gender"),
            "stock": item.get("stock_quantity", 0),
            "discount": item.get("discount_percentage", 0),
            "url": f"{settings.STORE_URL}/products/{item.get('slug', '')}",
            "image": _extract_image(item),
            "sizes": _extract_sizes(item),
            "colors": _extract_colors(item),
        })

    return json.dumps({
        "total": data.get("total", len(simplified)),
        "products": simplified,
    })


async def get_product_details(product_id: Optional[int] = None, slug: Optional[str] = None) -> str:
    """
    Get full details for a specific product by ID or slug.
    """
    if slug:
        data = await _api_get(f"/products/slug/{slug}")
    elif product_id:
        data = await _api_get(f"/products/{product_id}")
    else:
        return json.dumps({"error": "Provide either product_id or slug"})

    if "error" in data:
        return json.dumps(data)

    return json.dumps({
        "id": data.get("id"),
        "title": data.get("title"),
        "slug": data.get("slug"),
        "description": data.get("description"),
        "price": data.get("base_price") or data.get("product_price_taka"),
        "brand": data.get("brand_name"),
        "type": data.get("product_type"),
        "gender": data.get("gender"),
        "stock": data.get("stock_quantity", 0),
        "discount": data.get("discount_percentage", 0),
        "specification": data.get("product_specification"),
        "url": f"{settings.STORE_URL}/products/{data.get('slug', '')}",
        "image": _extract_image(data),
        "sizes": _extract_sizes(data),
        "colors": _extract_colors(data),
        "variants": [
            {
                "size": v.get("size"),
                "color": v.get("color"),
                "price": v.get("price"),
                "stock": v.get("stock_quantity", 0),
            }
            for v in data.get("variants", [])
        ],
    })


async def check_order_status(order_id: int) -> str:
    """
    Check the status of a customer order by order ID.
    """
    data = await _api_get(f"/orders/{order_id}")

    if "error" in data:
        return json.dumps({
            "error": f"Could not find order #{order_id}. Please check the order number and try again."
        })

    return json.dumps({
        "order_id": data.get("id"),
        "status": data.get("status"),
        "total": data.get("total_amount"),
        "payment_method": data.get("payment_method"),
        "payment_status": data.get("payment_status"),
        "city": data.get("city"),
        "items": [
            {
                "title": item.get("product_title"),
                "quantity": item.get("quantity"),
                "price": item.get("unit_price"),
            }
            for item in data.get("items", [])
        ],
        "created_at": data.get("created_at"),
    })


async def get_store_info(topic: str) -> str:
    """
    Get store information based on topic.
    Topics: return_policy, shipping, payment, contact, general
    """
    info: Dict[str, Any] = {
        "store_name": settings.STORE_NAME,
        "store_url": settings.STORE_URL,
    }

    if topic in ("return_policy", "shipping", "payment", "general"):
        # Try FAQs endpoint
        faq_data = await _api_get("/faqs")
        if isinstance(faq_data, list):
            # Filter FAQs relevant to the topic
            relevant = [
                {"question": f.get("question"), "answer": f.get("answer")}
                for f in faq_data
                if topic.lower() in (f.get("category", "") or "").lower()
                or topic.lower() in (f.get("question", "") or "").lower()
            ]
            info["faqs"] = relevant if relevant else faq_data[:5]

        # Try policies endpoint
        policy_data = await _api_get("/policies")
        if isinstance(policy_data, list):
            relevant_policies = [
                {"title": p.get("title"), "content": p.get("content")}
                for p in policy_data
                if topic.lower() in (p.get("title", "") or "").lower()
                or topic.lower() in (p.get("slug", "") or "").lower()
            ]
            if relevant_policies:
                info["policies"] = relevant_policies

    if topic == "contact":
        info["contact"] = {
            "website": settings.STORE_URL,
            "message": "You can reach us through this chat or visit our website.",
        }

    return json.dumps(info)


async def collect_lead(
    name: str,
    phone: str,
    interest: str,
    platform: str = "unknown",
    user_id: str = "",
) -> str:
    """
    Save customer info for human follow-up.
    Stores to local JSON file and Supabase if configured.
    """
    lead = {
        "name": name,
        "phone": phone,
        "interest": interest,
        "platform": platform,
        "user_id": user_id,
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
    }

    # Save to local file (fallback/backup)
    leads_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "leads")
    os.makedirs(leads_dir, exist_ok=True)
    leads_file = os.path.join(leads_dir, "leads.jsonl")

    try:
        with open(leads_file, "a") as f:
            f.write(json.dumps(lead) + "\n")
    except Exception as e:
        logger.error(f"Failed to write lead to local file: {e}")

    # Save to Supabase
    from database import db
    if db.is_configured():
        try:
            await db.save_lead(platform, user_id, name, phone, interest)
            logger.info(f"Lead saved to Supabase: {name} ({phone})")
        except Exception as e:
            logger.error(f"Failed to save lead to Supabase: {e}")

    logger.info(f"Lead collected: {name} ({phone}) — {interest}")

    return json.dumps({
        "success": True,
        "message": f"Thank you {name}! We've noted your interest in {interest}. Our team will contact you at {phone} soon.",
    })


async def request_human_agent(reason: str) -> str:
    """
    Flag the conversation for human agent handoff.
    """
    logger.info(f"Human handoff requested: {reason}")

    return json.dumps({
        "success": True,
        "message": "I'm connecting you with a human agent. Someone from our team will be with you shortly. In the meantime, feel free to browse our store at " + settings.STORE_URL,
    })


async def _send_platform_text(platform: str, recipient_id: str, text: str) -> None:
    """Helper to send text to a specific user on a specific platform (messenger or whatsapp)."""
    if not recipient_id:
        return
    try:
        if platform == "messenger":
            from messaging import messenger_api
            await messenger_api.send_text_chunked(recipient_id, text)
        elif platform == "whatsapp":
            from messaging import whatsapp_api
            await whatsapp_api.send_text_chunked(recipient_id, text)
        else:
            logger.warning(f"Unknown messaging platform '{platform}' for user '{recipient_id}'")
    except Exception as e:
        logger.error(f"Failed to send message to {platform}:{recipient_id}: {e}")


async def confirm_order(
    name: str,
    phone: str,
    address: str,
    product_name: str,
    variant: str,
    size: str,
    quantity: int,
    unit_price: float,
    total_price: float,
    customer_comment: Optional[str] = None,
    platform: str = "unknown",
    user_id: str = "",
) -> str:
    """
    Save a confirmed customer order with structured details, notify admins,
    and send a copy of the order + thank you message to the customer.
    """
    order_details_summary = f"Product: {product_name} | Variant: {variant} | Size: {size} | Qty: {quantity} | Unit Price: {unit_price} | Total: {total_price}"
    if customer_comment:
        order_details_summary += f" | Comment: {customer_comment}"

    order = {
        "name": name,
        "phone": phone,
        "address": address,
        "product_name": product_name,
        "variant": variant,
        "size": size,
        "quantity": quantity,
        "unit_price": unit_price,
        "total_price": total_price,
        "customer_comment": customer_comment,
        "order_details": order_details_summary,
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
    }

    # Save to local file
    orders_file = os.path.join(os.path.dirname(os.path.dirname(__file__)), "confirmed_orders.jsonl")
    try:
        with open(orders_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(order, ensure_ascii=False) + "\n")
    except Exception as e:
        logger.error(f"Failed to write order to local file: {e}")

    # Save to Supabase
    from database import db
    if db.is_configured():
        try:
            await db.save_order(platform, user_id, name, phone, address, order_details_summary)
            logger.info(f"Order saved to Supabase for {name}")
        except Exception as e:
            logger.error(f"Failed to save order to Supabase: {e}")

    logger.info(f"Order confirmed: {name} ({phone}) — {order_details_summary}")

    # Format messages
    order_time = order["timestamp"]
    comment_text = customer_comment if customer_comment else "None"
    
    admin_notification = (
        f"🛒 New Order Received\n\n"
        f"👤 Customer: {name}\n"
        f"📞 Phone: {phone}\n"
        f"📍 Address: {address}\n\n"
        f"📦 Product: {product_name}\n"
        f"🎨 Variant/Color: {variant}\n"
        f"📏 Size: {size}\n"
        f"🔢 Quantity: {quantity}\n\n"
        f"💰 Unit Price: {unit_price}\n"
        f"💵 Total Price: {total_price}\n\n"
        f"📝 Customer Comment:\n"
        f"{comment_text}\n\n"
        f"⏰ Order Time: {order_time}\n\n"
        f"Please process this order as soon as possible."
    )

    customer_copy = (
        f"📦 Order Details Confirmation\n\n"
        f"👤 Name: {name}\n"
        f"📞 Phone: {phone}\n"
        f"📍 Delivery Address: {address}\n\n"
        f"Product: {product_name}\n"
        f"Variant/Color: {variant}\n"
        f"Size: {size}\n"
        f"Quantity: {quantity}\n"
        f"Unit Price: {unit_price} BDT\n"
        f"Total Price: {total_price} BDT\n"
    )
    if customer_comment:
        customer_copy += f"Comment: {customer_comment}\n"

    thank_you_msg = (
        f"Thank you for your order, {name}! "
        f"We have received your order details and will process it shortly. "
        f"We appreciate your business! 😊"
    )

    # 1. Notify only Admin users
    admin_users = []
    if db.is_configured():
        try:
            admin_users = await db.get_admin_users()
        except Exception as e:
            logger.error(f"Failed to get admin users: {e}")

    # If no admins are found or database is offline, fall back to default Admin (Dip Durlov)
    if not admin_users:
        logger.warning("No admin users found in database or DB offline. Falling back to default admin Dip Durlov.")
        admin_users = [{
            "platform": "messenger",
            "user_id": "26761204070240994",
            "name": "Dip Durlov"
        }]

    for admin in admin_users:
        admin_platform = admin.get("platform")
        admin_user_id = admin.get("user_id")
        if admin_platform and admin_user_id:
            logger.info(f"Sending order notification to Admin {admin.get('name')} ({admin_platform}:{admin_user_id})")
            await _send_platform_text(admin_platform, admin_user_id, admin_notification)

    # 2. Notify Customer
    if platform != "unknown" and user_id:
        logger.info(f"Sending order copy and thank-you message to customer ({platform}:{user_id})")
        await _send_platform_text(platform, user_id, customer_copy)
        await _send_platform_text(platform, user_id, thank_you_msg)

    return json.dumps({
        "success": True,
        "message": f"Your order has been confirmed successfully! Thank you {name}. We will process it shortly.",
    }, ensure_ascii=False)


async def update_customer_profile(
    name: Optional[str] = None,
    phone: Optional[str] = None,
    address: Optional[str] = None,
    platform: str = "unknown",
    user_id: str = "",
) -> str:
    """
    Update details about the customer (name, phone, shipping address) in Supabase.
    """
    from database import db
    if not db.is_configured():
        return json.dumps({"success": False, "error": "Database not configured"})

    updates = {}
    if name:
        updates["name"] = name
    if phone:
        updates["phone"] = phone
    if address:
        updates["address"] = address

    if not updates:
        return json.dumps({"success": True, "message": "No profile updates provided."})

    try:
        await db.create_or_update_user(platform, user_id, **updates)
        logger.info(f"Updated profile for {platform}:{user_id}: {updates}")
        return json.dumps({
            "success": True,
            "message": f"Customer profile updated with: {', '.join(updates.keys())}"
        })
    except Exception as e:
        logger.error(f"Failed to update profile for {platform}:{user_id}: {e}")
        return json.dumps({"success": False, "error": str(e)})


async def save_customer_fact(
    fact: str,
    platform: str = "unknown",
    user_id: str = "",
) -> str:
    """
    Save or append a specific preference or detail about the customer to their notes in Supabase.
    """
    from database import db
    if not db.is_configured():
        return json.dumps({"success": False, "error": "Database not configured"})

    try:
        existing_facts = await db.get_context(platform, user_id, "user_facts")
        if existing_facts:
            cleaned_facts = existing_facts.strip()
            if not cleaned_facts.startswith("-"):
                cleaned_facts = f"- {cleaned_facts}"
            
            if fact.lower() in cleaned_facts.lower():
                return json.dumps({"success": True, "message": "Fact already remembered."})
                
            updated_facts = cleaned_facts + f"\n- {fact}"
        else:
            updated_facts = f"- {fact}"

        await db.save_context(platform, user_id, "user_facts", updated_facts)
        logger.info(f"Saved fact for {platform}:{user_id}: {fact}")
        return json.dumps({
            "success": True,
            "message": f"Remembered fact: {fact}"
        })
    except Exception as e:
        logger.error(f"Failed to save customer fact for {platform}:{user_id}: {e}")
        return json.dumps({"success": False, "error": str(e)})


# ── Tool Dispatch ────────────────────────────────────────────────────────

TOOL_MAP = {
    "search_products": search_products,
    "get_product_details": get_product_details,
    "check_order_status": check_order_status,
    "get_store_info": get_store_info,
    "collect_lead": collect_lead,
    "request_human_agent": request_human_agent,
    "confirm_order": confirm_order,
    "update_customer_profile": update_customer_profile,
    "save_customer_fact": save_customer_fact,
}


async def execute_tool(tool_name: str, arguments: Dict) -> str:
    """
    Execute a tool call and return the result as a JSON string.
    This is called by the AI agent when it decides to use a tool.
    """
    func = TOOL_MAP.get(tool_name)
    if not func:
        return json.dumps({"error": f"Unknown tool: {tool_name}"})

    try:
        result = await func(**arguments)
        return result
    except TypeError as e:
        logger.error(f"Tool {tool_name} argument error: {e}")
        return json.dumps({"error": f"Invalid arguments for {tool_name}: {e}"})
    except Exception as e:
        logger.error(f"Tool {tool_name} execution error: {e}")
        return json.dumps({"error": f"Tool execution failed: {e}"})


# ── Helpers ──────────────────────────────────────────────────────────────


def _extract_image(product: Dict) -> Optional[str]:
    """Extract best available image URL from product data."""
    for field in ("variant_base_image_url", "baseImageUrl"):
        url = product.get(field)
        if url and isinstance(url, str) and url.startswith("http"):
            return url

    images = product.get("images")
    if not images:
        return None

    if isinstance(images, str):
        if images.startswith("http"):
            return images
        try:
            images = json.loads(images)
        except (ValueError, TypeError):
            return None

    if isinstance(images, list) and images:
        first = images[0]
        if isinstance(first, str) and first.startswith("http"):
            return first
        if isinstance(first, dict):
            return first.get("url") or first.get("image", {}).get("url")

    return None


def _extract_sizes(product: Dict) -> List[str]:
    """Extract available sizes from product variants."""
    sizes = set()
    for v in product.get("variants", []):
        s = v.get("size")
        if s:
            sizes.add(s)

    # Fallback to size_variants / sizeVariants JSON
    for field in ("size_variants", "sizeVariants"):
        raw = product.get(field)
        if raw:
            try:
                data = json.loads(raw) if isinstance(raw, str) else raw
                if isinstance(data, list):
                    for item in data:
                        if isinstance(item, str):
                            sizes.add(item)
            except (ValueError, TypeError):
                pass

    return sorted(sizes)


def _extract_colors(product: Dict) -> List[str]:
    """Extract available colors from product variants."""
    colors = set()
    for v in product.get("variants", []):
        c = v.get("color")
        if c:
            colors.add(c)

    for field in ("color_variants", "colorVariants"):
        raw = product.get(field)
        if raw:
            try:
                data = json.loads(raw) if isinstance(raw, str) else raw
                if isinstance(data, list):
                    for item in data:
                        if isinstance(item, str):
                            colors.add(item)
                        elif isinstance(item, dict):
                            for v in item.values():
                                if isinstance(v, str) and v.strip():
                                    colors.add(v.strip())
            except (ValueError, TypeError):
                pass

    return sorted(colors)
