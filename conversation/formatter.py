"""
Response Formatter
==================
Transforms AI text responses + tool results into platform-specific
rich messages (Messenger carousel cards, WhatsApp interactive lists, etc.).
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from config import settings


def format_product_for_messenger(product: Dict) -> Dict:
    """
    Convert a product dict into a Messenger Generic Template element.
    https://developers.facebook.com/docs/messenger-platform/send-messages/template/generic
    """
    price = product.get("base_price") or product.get("product_price_taka") or 0
    title = product.get("title", "Product")
    slug = product.get("slug", "")

    # Build image URL — prefer variant_base_image_url, then images
    image_url = _extract_image_url(product)

    # Product URL on the store
    product_url = f"{settings.STORE_URL}/products/{slug}" if slug else settings.STORE_URL

    element = {
        "title": f"{title}",
        "subtitle": f"৳{int(price)} {settings.STORE_CURRENCY}",
        "default_action": {
            "type": "web_url",
            "url": product_url,
        },
        "buttons": [
            {
                "type": "web_url",
                "url": product_url,
                "title": "🛒 View Product",
            },
            {
                "type": "postback",
                "title": "📋 Details",
                "payload": f"PRODUCT_DETAILS_{product.get('id', '')}",
            },
        ],
    }

    if image_url:
        element["image_url"] = image_url

    return element


def format_products_messenger(products: List[Dict]) -> List[Dict]:
    """Format a list of products as Messenger carousel elements (max 10)."""
    return [format_product_for_messenger(p) for p in products[:10]]


def format_product_for_whatsapp(product: Dict, index: int = 0) -> Dict:
    """
    Convert a product dict into a WhatsApp interactive list row.
    """
    price = product.get("base_price") or product.get("product_price_taka") or 0
    title = product.get("title", "Product")[:24]  # WhatsApp 24-char limit
    product_id = product.get("id", index)

    return {
        "id": f"product_{product_id}",
        "title": title,
        "description": f"৳{int(price)} — Tap for details",
    }


def format_products_whatsapp(products: List[Dict]) -> List[Dict]:
    """Format products as WhatsApp list rows (max 10)."""
    return [
        format_product_for_whatsapp(p, i)
        for i, p in enumerate(products[:10])
    ]


def format_order_status(order: Dict) -> str:
    """Format an order status into a human-readable message."""
    status_emoji = {
        "pending": "⏳",
        "pending_confirmation": "📞",
        "confirmed": "✅",
        "processing": "📦",
        "shipped": "🚚",
        "delivered": "🎉",
        "canceled": "❌",
        "returned": "↩️",
    }

    order_id = order.get("id", "Unknown")
    status = order.get("status", "unknown")
    emoji = status_emoji.get(status, "❓")
    total = order.get("total_amount", 0)
    items = order.get("items", [])
    item_count = len(items)

    lines = [
        f"{emoji} Order #{order_id}",
        f"Status: {status.replace('_', ' ').title()}",
        f"Total: ৳{int(total)}",
        f"Items: {item_count}",
    ]

    if status == "shipped":
        lines.append("Your order is on the way! 🎁")
    elif status == "delivered":
        lines.append("Your order has been delivered! Hope you love it! ❤️")
    elif status == "pending_confirmation":
        lines.append("We'll call you shortly to confirm your order.")
    elif status == "canceled":
        reason = order.get("cancellation_reason", "")
        if reason:
            lines.append(f"Reason: {reason}")

    return "\n".join(lines)


def format_product_details(product: Dict) -> str:
    """Format full product details as readable text."""
    title = product.get("title", "Product")
    price = product.get("base_price") or product.get("product_price_taka") or 0
    description = product.get("description", "")
    brand = product.get("brand_name", "")
    stock = product.get("stock_quantity", 0)
    slug = product.get("slug", "")
    discount = product.get("discount_percentage", 0)

    lines = [f"🏷️ {title}"]

    if brand:
        lines.append(f"Brand: {brand}")

    if discount and discount > 0:
        original = price / (1 - discount / 100)
        lines.append(f"Price: ৳{int(price)} (Original: ৳{int(original)}, {int(discount)}% OFF)")
    else:
        lines.append(f"Price: ৳{int(price)}")

    if description:
        # Truncate long descriptions
        desc = description[:200] + "..." if len(description) > 200 else description
        lines.append(f"\n{desc}")

    # Variants (sizes/colors)
    variants = product.get("variants", [])
    if variants:
        sizes = sorted({v.get("size", "") for v in variants if v.get("size")})
        colors = sorted({v.get("color", "") for v in variants if v.get("color")})
        if sizes:
            lines.append(f"Sizes: {', '.join(sizes)}")
        if colors:
            lines.append(f"Colors: {', '.join(colors)}")

    if stock > 0:
        if stock <= 10:
            lines.append(f"⚠️ Only {stock} left in stock!")
        else:
            lines.append("✅ In stock")
    else:
        lines.append("❌ Out of stock")

    if slug:
        lines.append(f"\n🔗 {settings.STORE_URL}/products/{slug}")

    return "\n".join(lines)


def _extract_image_url(product: Dict) -> Optional[str]:
    """Extract the best available image URL from a product dict."""
    # Try direct URL fields first
    for field in ("variant_base_image_url", "baseImageUrl"):
        url = product.get(field)
        if url and isinstance(url, str) and url.startswith("http"):
            return url

    # Try images field (could be JSON string or list)
    images = product.get("images")
    if not images:
        return None

    if isinstance(images, str):
        # Could be a direct URL or JSON
        if images.startswith("http"):
            return images
        try:
            import json
            images = json.loads(images)
        except (ValueError, TypeError):
            return None

    if isinstance(images, list) and images:
        first = images[0]
        if isinstance(first, str):
            return first if first.startswith("http") else None
        if isinstance(first, dict):
            return (
                first.get("url")
                or first.get("image", {}).get("url")
                or first.get("variantBaseImageUrl")
            )

    return None
