# 0009. Storefront: server-side backend calls, browser cart, PayPal pop-up
_Date: 2026-10-13 · Status: accepted_

## Context
D5 adds the storefront and shopping agent. The agent needs the Anthropic key, the backend's `/payments` and `/merchant`
endpoints carry no auth yet (ADRs 0006-0008), the cart has to live somewhere, and the PayPal JS SDK v6 offers several
presentation modes.

## Decision
- **The browser never calls the backend** (decided with Vic before the session). Next.js server code calls it at
  `BACKEND_URL`: the page reads `GET /catalog`, `/api/chat` runs the agent and its tools, `/api/orders/{id}/process`
  wraps `POST /merchant/process`, and `/api/orders/{id}/status` wraps the status endpoint. The proxies return only
  `{order_id, status}` or `{error: {code}}`.
- **Cart in the browser** (chosen with Vic). The page sends its cart and the persona key with each chat request; the
  `add_to_cart` tool returns the new cart and the page adopts it; `checkout` creates the order from that cart. The
  chat route looks the device ID up in `backend/app/personas.json` from the persona key, so the browser can't send
  an arbitrary device ID. The backend still prices every order.
- **Checkout without a confirmation turn** (chosen with Vic). "Buy X" runs search, add and checkout in one turn; the
  buyer's approval in PayPal is the human confirmation.
- **Personas read from `backend/app/personas.json`** (chosen with Vic); only key, name, device ID and expected status
  reach the browser.
- **Product images** (chosen with Vic): our own flat SVGs in `web/public/products/`, referenced by a new optional
  `image` field in `catalog.json`.
- **PayPal JS SDK v6 with the client ID and `presentationMode: "popup"`.** PayPal's guide uses the public client ID
  (no client token, so no new backend endpoint) and `"auto"`. In testing, `"auto"` tried the browser's PaymentRequest
  sheet; the brief has the buyer approve in the PayPal pop-up, so we ask for it. The agent has already created the
  order, so `start()` gets a settled promise with the PayPal order ID.
- The web app loads the repo-root `.env` itself (`web/lib/env.ts`), so one file serves backend and web.

## Alternatives considered
- Browser calls the backend directly with CORS: simpler, but D9 would need per-user auth on `/payments` and
  `/merchant` instead of keeping the backend private.
- Cart in a server session: survives a reload, but adds state that is lost on restart, for no demo benefit.
- `GET /personas` on the backend: cleaner if web and backend deploy from different checkouts; revisit in D9.

## Consequences
- D9 must deploy the web service with the repo's `backend/app/personas.json` and `prompts/` next to it (or add an
  endpoint), and set `BACKEND_URL` to the backend's private address.
- A reload clears the cart and chat; an order already created stays `created` until PayPal expires it.
- `/api/chat` is unauthenticated, so anyone who can reach the web app can spend model tokens and create sandbox
  orders. D9 should rate-limit it.
