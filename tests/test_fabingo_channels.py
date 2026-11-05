"""Smoke tests for Fabingo channel flags, secrets, and webhook scaffolding."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from config import FABINGO_PAGE_ID, settings
from engagement.memory import EngagementMemory, memory
from engagement.policy import decide_comment

ROOT = Path(__file__).resolve().parents[1]
# Real Graph user/page tokens are long. The prefix is split so this file does not
# contain a token-shaped literal.
GRAPH_TOKEN = re.compile("EAA" + r"[A-Za-z0-9]{20,}")


@pytest.fixture(autouse=True)
def _reset_engagement_memory():
    memory.clear()
    yield
    memory.clear()


def test_page_token_file_is_not_in_the_tree():
    assert not (ROOT / "facebook_pages.json").exists()
    example = json.loads((ROOT / "facebook_pages.example.json").read_text())
    token = example["data"][0]["access_token"]
    assert token == "PAGE_ACCESS_TOKEN_PLACEHOLDER"
    assert example["data"][0]["id"] == FABINGO_PAGE_ID
    assert "facebook_pages.json" in (ROOT / ".gitignore").read_text()


def test_working_tree_has_no_graph_user_tokens():
    allowed_suffixes = {".py", ".json", ".md", ".yml", ".yaml", ".html", ".txt", ".example"}
    for path in ROOT.rglob("*"):
        if not path.is_file():
            continue
        if ".git" in path.parts or "venv" in path.parts or ".venv" in path.parts:
            continue
        if path.suffix not in allowed_suffixes and path.name not in {".env.example", ".gitignore"}:
            continue
        text = path.read_text(errors="ignore")
        assert GRAPH_TOKEN.search(text) is None, path


def test_comment_policy_skips_the_page_and_answers_a_question():
    store = EngagementMemory()
    own = {
        "item": "comment",
        "verb": "add",
        "comment_id": "c1",
        "post_id": "p1",
        "message": "What is the price?",
        "from": {"id": FABINGO_PAGE_ID},
    }
    decision = decide_comment(own, FABINGO_PAGE_ID, store, now=1_000.0)
    assert decision.reply is False
    assert decision.like is False

    customer = {
        "item": "comment",
        "verb": "add",
        "comment_id": "c2",
        "post_id": "p1",
        "parent_id": "p1",
        "message": "What is the price of the black shirt?",
        "from": {"id": "customer-1"},
    }
    decision = decide_comment(customer, FABINGO_PAGE_ID, store, now=1_000.0)
    assert decision.reply is True
    assert decision.like is True

    nested = dict(customer, comment_id="c3", parent_id="c2")
    decision = decide_comment(nested, FABINGO_PAGE_ID, store, now=1_000.0)
    assert decision.reply is False
    assert "nested" in decision.reason

    heart = dict(customer, comment_id="c4", message="❤️")
    decision = decide_comment(heart, FABINGO_PAGE_ID, store, now=1_000.0)
    assert decision.reply is False
    assert decision.like is True


def test_health_separates_ai_keys_from_meta_tokens(monkeypatch):
    monkeypatch.setattr(settings, "GEMINI_API_KEY", "")
    monkeypatch.setattr(settings, "OPENAI_API_KEY", "")
    monkeypatch.setattr(settings, "GROQ_API_KEY", "")
    monkeypatch.setattr(settings, "MULTI_AI_API_URL_EXPLICIT", "")
    monkeypatch.setattr(settings, "META_PAGE_ACCESS_TOKEN", "")
    monkeypatch.setattr(settings, "ENABLE_MESSENGER", True)

    from main import app

    with TestClient(app) as client:
        payload = client.get("/health").json()

    assert payload["status"] == "degraded"
    assert payload["ai"]["can_generate_replies"] is False
    assert "GEMINI_API_KEY" in payload["ai"]["missing_keys"]
    assert payload["meta"]["page_access_token_set"] is False
    assert payload["channels"]["messenger"]["missing_tokens"] == ["META_PAGE_ACCESS_TOKEN"]
    assert payload["channels"]["instagram"]["enabled"] is False
    assert payload["channels"]["page_comments"]["enabled"] is False
    assert payload["fabingo"]["page_id"] == FABINGO_PAGE_ID
    assert GRAPH_TOKEN.search(json.dumps(payload)) is None
    joined = " ".join(payload["reasons"])
    assert "Meta tokens do not generate replies" in joined
    assert "META_PAGE_ACCESS_TOKEN" in joined


def test_test_message_refuses_without_an_ai_key(monkeypatch):
    monkeypatch.setattr(settings, "DEBUG", True)
    monkeypatch.setattr(settings, "GEMINI_API_KEY", "")
    monkeypatch.setattr(settings, "OPENAI_API_KEY", "")
    monkeypatch.setattr(settings, "GROQ_API_KEY", "")
    monkeypatch.setattr(settings, "MULTI_AI_API_URL_EXPLICIT", "")
    monkeypatch.setattr(settings, "META_PAGE_ACCESS_TOKEN", "test-page-token")

    from main import app

    with TestClient(app) as client:
        response = client.post("/test/message", json={"text": "price of black shirts"})

    body = response.json()
    assert response.status_code == 503
    assert body["meta_tokens"]["used"] is False
    assert body["meta_tokens"]["required"] is False
    assert body["meta_tokens"]["page_access_token_set"] is True
    assert body["missing_ai_keys"]


def test_webhook_verify_and_disabled_channels(monkeypatch):
    monkeypatch.setattr(settings, "META_VERIFY_TOKEN", "fashionarc_verify_2024")
    monkeypatch.setattr(settings, "META_APP_SECRET", "")
    monkeypatch.setattr(settings, "ENABLE_WHATSAPP", False)
    monkeypatch.setattr(settings, "ENABLE_INSTAGRAM", False)
    monkeypatch.setattr(settings, "ENABLE_PAGE_COMMENTS", False)
    monkeypatch.setattr(settings, "ENABLE_PAGE_REACTIONS", False)

    from main import app

    params = {
        "hub.mode": "subscribe",
        "hub.verify_token": "fashionarc_verify_2024",
        "hub.challenge": "challenge-ok",
    }
    with TestClient(app) as client:
        for path in ("/webhook/messenger", "/webhook/instagram", "/webhook/page"):
            response = client.get(path, params=params)
            assert response.status_code == 200, path
            assert response.text == "challenge-ok"

        whatsapp = client.get(
            "/webhook/whatsapp",
            params={**params, "hub.challenge": "98765"},
        )
        assert whatsapp.status_code == 200
        assert whatsapp.json() == 98765

        denied = client.get(
            "/webhook/instagram",
            params={**params, "hub.verify_token": "nope"},
        )
        assert denied.status_code == 403

        instagram = client.post(
            "/webhook/instagram",
            json={"object": "instagram", "entry": []},
        )
        assert instagram.json()["status"] == "disabled"

        whatsapp_post = client.post(
            "/webhook/whatsapp",
            json={"object": "whatsapp_business_account", "entry": []},
        )
        assert whatsapp_post.json()["status"] == "disabled"

        page = client.post(
            "/webhook/page",
            json={
                "object": "page",
                "entry": [
                    {
                        "id": FABINGO_PAGE_ID,
                        "changes": [
                            {
                                "field": "feed",
                                "value": {
                                    "item": "comment",
                                    "verb": "add",
                                    "comment_id": "c-disabled",
                                    "post_id": "p1",
                                    "message": "What is the price?",
                                    "from": {"id": "customer-9"},
                                },
                            }
                        ],
                    }
                ],
            },
        )
        assert page.status_code == 200
        assert page.json()["handled"][0]["action"] == "disabled"


def test_instagram_send_uses_page_token(monkeypatch):
    calls = []

    class FakeResponse:
        status_code = 200

        def json(self):
            return {"message_id": "m1"}

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, url, params=None, json=None, headers=None):
            calls.append({"url": url, "params": params, "json": json, "headers": headers})
            return FakeResponse()

    monkeypatch.setattr(settings, "META_PAGE_ACCESS_TOKEN", "test-page-token")
    monkeypatch.setattr(settings, "META_PAGE_ID", FABINGO_PAGE_ID)
    monkeypatch.setattr("messaging.instagram_api.httpx.AsyncClient", FakeClient)

    import asyncio
    from messaging.instagram_api import send_text

    asyncio.run(send_text("igsid-1", "hello"))
    assert calls[0]["params"]["access_token"] == "test-page-token"
    assert calls[0]["url"].endswith(f"/{FABINGO_PAGE_ID}/messages")
    assert calls[0]["json"]["recipient"]["id"] == "igsid-1"
    assert "messaging_type" not in calls[0]["json"]


def test_messenger_and_whatsapp_send_use_env_tokens(monkeypatch):
    calls = []

    class FakeResponse:
        status_code = 200

        def json(self):
            return {"message_id": "m1", "messages": [{"id": "m1"}]}

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, url, params=None, json=None, headers=None):
            calls.append({"url": url, "params": params, "json": json, "headers": headers})
            return FakeResponse()

    monkeypatch.setattr(settings, "META_PAGE_ACCESS_TOKEN", "test-page-token")
    monkeypatch.setattr(settings, "META_WHATSAPP_TOKEN", "test-wa-token")
    monkeypatch.setattr(settings, "META_WHATSAPP_PHONE_ID", "phone-1")
    monkeypatch.setattr("messaging.messenger_api.httpx.AsyncClient", FakeClient)
    monkeypatch.setattr("messaging.whatsapp_api.httpx.AsyncClient", FakeClient)
    monkeypatch.setattr("messaging.page_api.httpx.AsyncClient", FakeClient)

    import asyncio
    from messaging.messenger_api import send_text as messenger_send
    from messaging.page_api import like_object, reply_to_comment
    from messaging.whatsapp_api import send_text as whatsapp_send

    async def _send_all():
        await messenger_send("psid-1", "hi")
        await whatsapp_send("8801700000000", "hi")
        await reply_to_comment("comment-1", "Thanks, the shirt is ৳1200.")
        await like_object("comment-1")

    asyncio.run(_send_all())

    messenger_call, whatsapp_call, reply_call, like_call = calls
    assert messenger_call["params"]["access_token"] == "test-page-token"
    assert messenger_call["url"].endswith("/me/messages")
    assert whatsapp_call["headers"]["Authorization"] == "Bearer test-wa-token"
    assert whatsapp_call["url"].endswith("/phone-1/messages")
    assert reply_call["params"]["access_token"] == "test-page-token"
    assert reply_call["url"].endswith("/comment-1/comments")
    assert like_call["url"].endswith("/comment-1/likes")


def test_enabled_comment_replies_once_and_does_not_react_back(monkeypatch):
    replies = []
    likes = []

    async def fake_reply(comment_id, message):
        replies.append((comment_id, message))
        return {"id": "reply-1"}

    async def fake_like(object_id):
        likes.append(object_id)
        return {"success": True}

    async def fake_ai(messages, platform, user_id, use_tools=True):
        assert platform == "page_comment"
        assert use_tools is False
        return "The black shirt is ৳1200 on fa.bingo.", messages

    monkeypatch.setattr(settings, "ENABLE_PAGE_COMMENTS", True)
    monkeypatch.setattr(settings, "ENABLE_PAGE_REACTIONS", True)
    monkeypatch.setattr(settings, "META_PAGE_ID", FABINGO_PAGE_ID)
    monkeypatch.setattr(settings, "META_PAGE_ACCESS_TOKEN", "test-page-token")
    monkeypatch.setattr(settings, "GEMINI_API_KEY", "test-gemini-key")
    monkeypatch.setattr(settings, "MULTI_AI_API_URL_EXPLICIT", "")
    monkeypatch.setattr("messaging.page_api.reply_to_comment", fake_reply)
    monkeypatch.setattr("messaging.page_api.like_object", fake_like)
    monkeypatch.setattr("agent.salesman.get_ai_response", fake_ai)

    import asyncio
    from engagement.comments import handle_feed_value

    comment = {
        "item": "comment",
        "verb": "add",
        "comment_id": "c-live",
        "post_id": "post-1",
        "parent_id": "post-1",
        "message": "price of the black shirt?",
        "from": {"id": "customer-2", "name": "Ayesha"},
    }
    async def _run():
        first_result = await handle_feed_value(comment, FABINGO_PAGE_ID)
        second_result = await handle_feed_value(comment, FABINGO_PAGE_ID)
        reaction_result = await handle_feed_value(
            {"item": "reaction", "verb": "add", "reaction_type": "like", "post_id": "post-1"},
            FABINGO_PAGE_ID,
        )
        other_page_result = await handle_feed_value(dict(comment, comment_id="c-other"), "999")
        return first_result, second_result, reaction_result, other_page_result

    first, second, reaction, other_page = asyncio.run(_run())

    assert first["reply"] is True
    assert first["like"] is True
    assert len(replies) == 1
    assert likes == ["c-live"]
    assert second["reply"] is False
    assert reaction["action"] == "logged"
    assert other_page["action"] == "ignored"
    assert "different Page" in other_page["reason"]


def test_comment_reply_does_not_post_without_an_ai_key(monkeypatch):
    async def fake_reply(*args, **kwargs):
        raise AssertionError("should not post without an AI key")

    monkeypatch.setattr(settings, "ENABLE_PAGE_COMMENTS", True)
    monkeypatch.setattr(settings, "ENABLE_PAGE_REACTIONS", False)
    monkeypatch.setattr(settings, "META_PAGE_ID", FABINGO_PAGE_ID)
    monkeypatch.setattr(settings, "GEMINI_API_KEY", "")
    monkeypatch.setattr(settings, "OPENAI_API_KEY", "")
    monkeypatch.setattr(settings, "GROQ_API_KEY", "")
    monkeypatch.setattr(settings, "MULTI_AI_API_URL_EXPLICIT", "")
    monkeypatch.setattr("messaging.page_api.reply_to_comment", fake_reply)

    import asyncio
    from engagement.comments import handle_feed_value

    result = asyncio.run(
        handle_feed_value(
            {
                "item": "comment",
                "verb": "add",
                "comment_id": "c-noai",
                "post_id": "post-2",
                "message": "Do you have this in size M?",
                "from": {"id": "customer-3"},
            },
            FABINGO_PAGE_ID,
        )
    )
    assert result["reply"] is False
    assert "AI keys" in result["reply_skipped"]
