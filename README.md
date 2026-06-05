# 🤖 AI Salesman — Fashion ARC

Facebook Messenger & WhatsApp AI Sales Assistant for **Fashion ARC** (fa.bingo).

An intelligent chatbot that helps customers browse products, track orders, and get answers — all through Messenger and WhatsApp.

## Features

| Feature | Description |
|---------|-------------|
| 🔍 **Product Search** | Natural language search: "black oversized t-shirts under 1200" |
| 📋 **Product Details** | Full info: sizes, colors, price, stock, images |
| 📦 **Order Tracking** | Check order status by ID |
| ❓ **FAQ & Policies** | Return policy, shipping info, payment methods |
| 👤 **Lead Collection** | Saves customer info for human follow-up |
| 🤝 **Human Handoff** | Escalates to human agent when needed |
| 🔄 **AI Failover** | Gemini → OpenAI → Groq automatic switching |

## Architecture

```text
Messenger/WhatsApp User
        ↓
   Meta Webhooks (HTTPS)
        ↓
   AI Salesman (FastAPI :8001)
        ↓
   AI Agent (tool calling)
    ├── search_products  → Fashion ARC API
    ├── get_product_details → Fashion ARC API
    ├── check_order_status → Fashion ARC API
    ├── get_store_info → Fashion ARC API
    ├── collect_lead → Local storage
    └── request_human_agent → Flag conversation
        ↓
   Multi-AI API (Gemini/OpenAI/Groq)
        ↓
   Meta Send API → User
```

## Quick Start

### 1. Clone & Install

```bash
cd AI_Salesman
python -m venv venv
source venv/bin/activate  # macOS/Linux
pip install -r requirements.txt
```

### 2. Configure

```bash
cp .env.example .env
# Edit .env with your actual values
```

### 3. Run Locally

```bash
python main.py
```

Or:

```bash
uvicorn main:app --host 0.0.0.0 --port 8001 --reload
```

### 4. Test (No Meta Required)

```bash
# Health check
curl http://localhost:8001/health

# Test AI response
curl -X POST http://localhost:8001/test/message \
  -H "Content-Type: application/json" \
  -d '{"text": "Show me black t-shirts under 1200", "user_id": "test"}'
```

### 5. Expose for Meta Webhooks

For development, use ngrok:

```bash
ngrok http 8001
```

Copy the HTTPS URL (e.g., `https://abc123.ngrok.io`).

### 6. Configure Meta Webhooks

**Messenger:**
1. Go to [Meta Developer Console](https://developers.facebook.com/)
2. Your App → Messenger → Webhooks
3. Callback URL: `https://your-ngrok-url/webhook/messenger`
4. Verify Token: `fashionarc_verify_2024` (or your custom token)
5. Subscribe to: `messages`, `messaging_postbacks`

**WhatsApp:**
1. Your App → WhatsApp → Configuration
2. Callback URL: `https://your-ngrok-url/webhook/whatsapp`
3. Verify Token: same as above
4. Subscribe to: `messages`

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/` | Health check |
| `GET` | `/health` | Detailed status |
| `GET` | `/docs` | Swagger UI |
| `GET` | `/webhook/messenger` | Messenger verification |
| `POST` | `/webhook/messenger` | Receive Messenger messages |
| `GET` | `/webhook/whatsapp` | WhatsApp verification |
| `POST` | `/webhook/whatsapp` | Receive WhatsApp messages |
| `POST` | `/test/message` | Test AI (debug mode only) |
| `GET` | `/conversations` | List active sessions (debug) |

## Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `META_APP_SECRET` | Prod | For webhook signature verification |
| `META_VERIFY_TOKEN` | Yes | Webhook verification token |
| `META_PAGE_ACCESS_TOKEN` | Messenger | Facebook Page Access Token |
| `META_PAGE_ID` | Messenger | Facebook Page ID |
| `META_WHATSAPP_TOKEN` | WhatsApp | WhatsApp Business API token |
| `META_WHATSAPP_PHONE_ID` | WhatsApp | WhatsApp phone number ID |
| `FASHION_ARC_API_URL` | Yes | Fashion ARC backend URL |
| `MULTI_AI_API_URL` | No | Multi-AI API URL (fallback: direct Gemini) |
| `GEMINI_API_KEY` | Fallback | Direct Gemini API key |
| `STORE_NAME` | No | Store display name |
| `STORE_URL` | No | Store website URL |

## Project Structure

```
AI_Salesman/
├── main.py                      # FastAPI entry point
├── config.py                    # Environment config
├── requirements.txt             # Dependencies
├── .env.example                 # Template env vars
│
├── webhooks/
│   ├── messenger.py             # Messenger webhook handler
│   └── whatsapp.py              # WhatsApp webhook handler
│
├── messaging/
│   ├── messenger_api.py         # Messenger Send API wrapper
│   └── whatsapp_api.py          # WhatsApp Cloud API wrapper
│
├── agent/
│   ├── salesman.py              # AI agent (system prompt + tools)
│   └── tool_executor.py         # Tool execution (API calls)
│
└── conversation/
    ├── manager.py               # Session & history management
    └── formatter.py             # Platform-specific formatting
```

## Tech Stack

- **FastAPI** — async Python web framework
- **httpx** — async HTTP client
- **Gemini / OpenAI / Groq** — AI providers (via Multi-AI API)
- **Fashion ARC API** — product catalog, orders, FAQs
- **Meta Graph API v21.0** — Messenger & WhatsApp

## License

MIT
