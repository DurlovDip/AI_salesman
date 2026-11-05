# Fabingo Page setup

This service is the omnichannel bot for the **Fabingo** Facebook Page (Fashion ARC, [fa.bingo](https://fa.bingo)). It can take Messenger DMs, WhatsApp, Instagram DMs, public comment replies, and a single like on a customer comment. Each surface has its own env flag and stays off until you turn it on.

The Meta app stays in **Development**. Do not switch it to Live, and do not submit App Review, from this repo.

## Identifiers

| What | Value |
| --- | --- |
| Meta app | Fabingo `2331344683941720` (Development) |
| Facebook Page | Fabingo `970073619531937` |
| Store | Fashion ARC, https://fa.bingo |

`META_PAGE_ID` defaults to the Fabingo Page id. Override it only if you mean a different Page.

## Revoke the tokens that were committed

`facebook_pages.json` was committed in the initial commit (`64a45e2`) and contained live Page access tokens. This change deletes the file and ignores it. **Git history was not rewritten.** A history rewrite would need a force-push on `main`, which is not safe to do from a pull request. Anyone who can read the old commit can still see those tokens. Treat them as public.

Revoke every token from that file, then issue new ones and store them only in the host's env:

| Page | Id |
| --- | --- |
| Fabingo | `970073619531937` |
| Octagon | `371706709362396` |
| যুগযান | `110375994029634` |
| চারু চিরকুট | `2035630433408147` |

Practical reset, per Page:

1. Open [Meta Business Integrations](https://www.facebook.com/settings?tab=business_tools) (and the Page's connected apps) and remove the Fabingo app, or remove the system user that minted the token.
2. In the [app dashboard](https://developers.facebook.com/apps/2331344683941720/), generate a new Page access token for Fabingo only.
3. Put it in `META_PAGE_ACCESS_TOKEN` on the host. Do not commit it, and do not put it back in `facebook_pages.json`.
4. Repeat for the other three Pages if those tokens should die too. This bot should only receive a token for Fabingo.

`facebook_pages.example.json` is the only page file that belongs in git. It has a placeholder, not a token.

## Env checklist

Copy `.env.example` to `.env`. Minimum to answer a Messenger DM:

| Variable | Role |
| --- | --- |
| `META_VERIFY_TOKEN` | Same string you type in every Meta webhook form |
| `META_APP_SECRET` | Checks `X-Hub-Signature-256`. Set this on any public host |
| `META_PAGE_ACCESS_TOKEN` | Fabingo Page token. Sends Messenger, Instagram, comment replies, and likes |
| `META_PAGE_ID` | `970073619531937` |
| One of `GEMINI_API_KEY`, `OPENAI_API_KEY`, `GROQ_API_KEY` | Actually writes the reply |

Meta tokens and AI keys fail independently. `/health` reports both. `/test/message` calls the model only. It does not use Meta tokens, and it returns 503 when no AI key is set instead of a fake answer. The localhost `MULTI_AI_API_URL` default is not treated as configured. Set `MULTI_AI_API_URL` only when that service is running.

| Flag | Default | What it turns on |
| --- | --- | --- |
| `ENABLE_MESSENGER` | `true` | Messenger DMs on `/webhook/messenger` |
| `ENABLE_WHATSAPP` | `false` | WhatsApp Cloud API. Code is in place; leave off until you have tested a real number |
| `ENABLE_INSTAGRAM` | `false` | Instagram Messaging DMs |
| `ENABLE_PAGE_COMMENTS` | `false` | Public replies to new top-level comments |
| `ENABLE_PAGE_REACTIONS` | `false` | One like on a customer comment. Incoming reactions are logged and not answered |
| `ENABLE_MESSENGER_POLLING` | `false` | Graph polling fallback. Leave off and use webhooks |

WhatsApp also needs `META_WHATSAPP_TOKEN` and `META_WHATSAPP_PHONE_ID`. Instagram send uses the Page token. `META_IG_ID` is the linked Instagram professional account id and is optional for receiving.

## Webhooks

Replace `<host>` with the Render URL or an ngrok URL. All four forms use `META_VERIFY_TOKEN`.

| Product in the app dashboard | Callback URL | Fields |
| --- | --- | --- |
| Messenger | `https://<host>/webhook/messenger` | `messages`, `messaging_postbacks`, `message_deliveries`, `message_reads` |
| WhatsApp | `https://<host>/webhook/whatsapp` | `messages` |
| Instagram | `https://<host>/webhook/instagram` | `messages`, `messaging_postbacks`, `messaging_seen` |
| Webhooks → Page | `https://<host>/webhook/page` | `feed` |

Subscribe the **Fabingo** Page to each product you enable. `feed` on the Page object is what delivers comments and reactions, so comment automation does not poll the Graph.

`/webhook/messenger` also accepts a `feed` change if you point the Page subscription at that URL instead. Use only one of the two URLs for `feed`, or the same comment can be handled twice.

`GET /test-webhook` prints the four URLs for the current host. It does not print the verify token.

## What the Page bot will and will not do

Comments (`ENABLE_PAGE_COMMENTS=true`):

- Replies only to a new top-level comment on the configured Page.
- Skips the Page's own comments and nested replies.
- Skips emoji-only and very short comments.
- Caps replies at 6 per post per hour.
- Needs an AI key. If the key is missing it does not post a canned comment.
- The model is called with tools disabled, so a public comment cannot place an order.
- Permission: `pages_manage_engagement` (read engagement as well: `pages_read_engagement`).

Likes (`ENABLE_PAGE_REACTIONS=true`):

- Likes a customer comment once, including a short "nice" or a heart, and does not like complaints.
- Does not like or react back when someone reacts to a post. That loop is spam.
- Caps likes at 30 per hour.
- Uses `POST /{comment-id}/likes`.

Instagram (`ENABLE_INSTAGRAM=true`):

- Verifies the webhook, reads `object: "instagram"` messaging events, and sends with `POST /{PAGE_ID}/messages` and the Page token.
- This is the same shape as Messenger, with less of the Messenger template surface. Quick replies and private replies from comments are not implemented.
- Product to add on the app: Instagram → Instagram Messaging, with the Fabingo Page's Instagram professional account linked.
- Permissions to request later, still in Development: `instagram_manage_messages`, `instagram_basic`, `pages_show_list`, `pages_manage_metadata`.

Messenger and WhatsApp send paths read `META_PAGE_ACCESS_TOKEN` and `META_WHATSAPP_TOKEN` from the environment. They do not read `facebook_pages.json`.

## Development mode vs Live

In **Development**, only people with a role on the Fabingo app (admin, developer, tester) can message the Page, comment in a way the app can see, or use Instagram Messaging. Add testers in the app dashboard. You do not need Business Verification for that.

**Live** is a later, manual step for real customers:

- Business Verification on the Meta business that owns the app.
- App Review for the permissions above, plus `pages_messaging` for Messenger and the WhatsApp business permissions.
- Advanced Access, then switch the app to Live.

Do those in the Meta dashboard when you are ready. Nothing in this service flips that switch.

## Deploy on Render

`ai-salesman-rho.vercel.app` is not the host anymore. The repo has `render.yaml` and a `Procfile`.

1. Create a Render web service from this repo. `render.yaml` sets the start command and the safe flag defaults. It does not contain secrets (`sync: false` on tokens and AI keys).
2. In the Render dashboard, set `META_APP_SECRET`, `META_VERIFY_TOKEN`, `META_PAGE_ACCESS_TOKEN`, and one AI key. `META_PAGE_ID` is already `970073619531937`.
3. Leave `ENABLE_WHATSAPP`, `ENABLE_INSTAGRAM`, `ENABLE_PAGE_COMMENTS`, and `ENABLE_PAGE_REACTIONS` false until that product is configured on the app.
4. Set `DEBUG=false` on Render (already the value in `render.yaml`).
5. Use `https://<service>.onrender.com` as `<host>` in the webhook table.

Do not put API keys in git to make the deploy work.

## Local smoke

```bash
pip install -r requirements.txt pytest
pytest -q
uvicorn main:app --host 0.0.0.0 --port 8001
curl -s http://127.0.0.1:8001/health | python -m json.tool
```

`/health` is `degraded` until an AI key is present, and it names a missing Page token separately when a channel is enabled. With no AI key, `POST /test/message` returns 503 and says Meta tokens were not used.
