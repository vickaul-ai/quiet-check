# web

Next.js app for the Quiet Check storefront and shopping agent (D5) and, from D6, the merchant console. Its server code
calls the backend at `BACKEND_URL` (default `http://127.0.0.1:8000`); the browser only calls this app's `/api/*` routes
(see `../docs/contracts.md`, "Storefront routes", and ADR 0009). It reads the repo-root `.env`.

Run it from the repo root with `make dev` (it needs the backend and members), or here with `pnpm dev`.
`pnpm lint` and `pnpm typecheck` run in CI.

The LLM is created in one place, `lib/model.ts`, so the provider can be swapped by env var. The shopping agent's prompt
is `../prompts/shopping_agent.md`; the personas come from `../backend/app/personas.json`.
