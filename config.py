"""
Centralized configuration for the AI Salesman service.
All settings are loaded from environment variables.
"""

import os
from dotenv import load_dotenv

load_dotenv()

# Public identifiers for the Fabingo Meta app and Page. These are not secrets.
FABINGO_APP_ID = "2331344683941720"
FABINGO_PAGE_ID = "970073619531937"


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


class Settings:
    """Application settings loaded from environment variables."""

    # ── Meta Platform ─────────────────────────────────────────────────────
    META_APP_ID: str = os.getenv("META_APP_ID", FABINGO_APP_ID)
    META_APP_SECRET: str = os.getenv("META_APP_SECRET", "")
    META_VERIFY_TOKEN: str = os.getenv("META_VERIFY_TOKEN", "fashionarc_verify_2024")

    # Messenger / Page (Fabingo is the default Page id; override with META_PAGE_ID)
    META_PAGE_ACCESS_TOKEN: str = os.getenv("META_PAGE_ACCESS_TOKEN", "")
    META_PAGE_ID: str = os.getenv("META_PAGE_ID", FABINGO_PAGE_ID)

    # Instagram professional account linked to the Page. Send API can use the Page token.
    META_IG_ID: str = os.getenv("META_IG_ID", "")

    # WhatsApp
    META_WHATSAPP_TOKEN: str = os.getenv("META_WHATSAPP_TOKEN", "")
    META_WHATSAPP_PHONE_ID: str = os.getenv("META_WHATSAPP_PHONE_ID", "")

    # Independent channel switches. New and untested surfaces stay off.
    ENABLE_MESSENGER: bool = _env_bool("ENABLE_MESSENGER", True)
    ENABLE_WHATSAPP: bool = _env_bool("ENABLE_WHATSAPP", False)
    ENABLE_INSTAGRAM: bool = _env_bool("ENABLE_INSTAGRAM", False)
    ENABLE_PAGE_COMMENTS: bool = _env_bool("ENABLE_PAGE_COMMENTS", False)
    ENABLE_PAGE_REACTIONS: bool = _env_bool("ENABLE_PAGE_REACTIONS", False)
    # Graph polling is a dev fallback only. Webhooks are the real path.
    ENABLE_MESSENGER_POLLING: bool = _env_bool("ENABLE_MESSENGER_POLLING", False)

    # ── External APIs ─────────────────────────────────────────────────────
    FASHION_ARC_API_URL: str = os.getenv(
        "FASHION_ARC_API_URL", "https://fashionarc-backend.vercel.app"
    )
    # Empty means "not configured". The localhost default is only a client fallback
    # and must not be reported as a working AI provider.
    MULTI_AI_API_URL_EXPLICIT: str = os.getenv("MULTI_AI_API_URL", "").strip()
    MULTI_AI_API_URL: str = MULTI_AI_API_URL_EXPLICIT or "http://localhost:8000"

    # ── Direct AI Keys (fallback if Multi-AI API is not running) ──────────
    GEMINI_API_KEY: str = os.getenv("GEMINI_API_KEY", "")
    OPENAI_API_KEY: str = os.getenv("OPENAI_API_KEY", "")
    GROQ_API_KEY: str = os.getenv("GROQ_API_KEY", "")

    # ── Store Branding ────────────────────────────────────────────────────
    STORE_NAME: str = os.getenv("STORE_NAME", "Fashion ARC")
    STORE_URL: str = os.getenv("STORE_URL", "https://fa.bingo")
    STORE_CURRENCY: str = os.getenv("STORE_CURRENCY", "BDT")

    # ── Conversation Settings ─────────────────────────────────────────────
    SESSION_TIMEOUT_MINUTES: int = int(os.getenv("SESSION_TIMEOUT_MINUTES", "30"))
    MAX_HISTORY_MESSAGES: int = int(os.getenv("MAX_HISTORY_MESSAGES", "20"))

    # ── AI Model Settings ─────────────────────────────────────────────────
    PRIMARY_AI_PROVIDER: str = os.getenv("PRIMARY_AI_PROVIDER", "gemini")

    # ── Server ────────────────────────────────────────────────────────────
    HOST: str = os.getenv("HOST", "0.0.0.0")
    PORT: int = int(os.getenv("PORT", "8001"))
    DEBUG: bool = os.getenv("DEBUG", "true").lower() == "true"

    # ── Supabase ──────────────────────────────────────────────────────────
    SUPABASE_URL: str = os.getenv("SUPABASE_URL", "")
    SUPABASE_KEY: str = os.getenv("SUPABASE_KEY", "")

    @property
    def supabase_configured(self) -> bool:
        return bool(self.SUPABASE_URL and self.SUPABASE_KEY)

    @property
    def messenger_configured(self) -> bool:
        return bool(self.META_PAGE_ACCESS_TOKEN)

    @property
    def whatsapp_configured(self) -> bool:
        return bool(self.META_WHATSAPP_TOKEN and self.META_WHATSAPP_PHONE_ID)

    @property
    def instagram_configured(self) -> bool:
        # Instagram Messaging with a linked Page uses the Page access token.
        return bool(self.META_PAGE_ACCESS_TOKEN)

    @property
    def page_engagement_configured(self) -> bool:
        return bool(self.META_PAGE_ACCESS_TOKEN and self.META_PAGE_ID)

    @property
    def direct_ai_providers(self) -> dict:
        return {
            "openai": bool(self.OPENAI_API_KEY),
            "gemini": bool(self.GEMINI_API_KEY),
            "groq": bool(self.GROQ_API_KEY),
        }

    @property
    def multi_ai_configured(self) -> bool:
        return bool(self.MULTI_AI_API_URL_EXPLICIT)

    @property
    def ai_reply_ready(self) -> bool:
        """True when some AI backend can actually generate a reply."""
        return any(self.direct_ai_providers.values()) or self.multi_ai_configured

    @property
    def missing_ai_keys(self) -> list:
        if self.ai_reply_ready:
            return []
        return ["GEMINI_API_KEY", "OPENAI_API_KEY", "GROQ_API_KEY"]

    @property
    def messenger_enabled(self) -> bool:
        return self.ENABLE_MESSENGER

    @property
    def whatsapp_enabled(self) -> bool:
        return self.ENABLE_WHATSAPP

    @property
    def instagram_enabled(self) -> bool:
        return self.ENABLE_INSTAGRAM

    @property
    def page_comments_enabled(self) -> bool:
        return self.ENABLE_PAGE_COMMENTS

    @property
    def page_reactions_enabled(self) -> bool:
        return self.ENABLE_PAGE_REACTIONS

    @property
    def messenger_polling_enabled(self) -> bool:
        return self.ENABLE_MESSENGER_POLLING


settings = Settings()
