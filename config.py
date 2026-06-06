"""
Centralized configuration for the AI Salesman service.
All settings are loaded from environment variables.
"""

import os
from dotenv import load_dotenv

load_dotenv()


class Settings:
    """Application settings loaded from environment variables."""

    # ── Meta Platform ─────────────────────────────────────────────────────
    META_APP_SECRET: str = os.getenv("META_APP_SECRET", "")
    META_VERIFY_TOKEN: str = os.getenv("META_VERIFY_TOKEN", "fashionarc_verify_2024")

    # Messenger
    META_PAGE_ACCESS_TOKEN: str = os.getenv("META_PAGE_ACCESS_TOKEN", "")
    META_PAGE_ID: str = os.getenv("META_PAGE_ID", "")

    # WhatsApp
    META_WHATSAPP_TOKEN: str = os.getenv("META_WHATSAPP_TOKEN", "")
    META_WHATSAPP_PHONE_ID: str = os.getenv("META_WHATSAPP_PHONE_ID", "")

    # ── External APIs ─────────────────────────────────────────────────────
    FASHION_ARC_API_URL: str = os.getenv(
        "FASHION_ARC_API_URL", "https://fashionarc-backend.vercel.app"
    )
    MULTI_AI_API_URL: str = os.getenv(
        "MULTI_AI_API_URL", "http://localhost:8000"
    )

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


settings = Settings()
