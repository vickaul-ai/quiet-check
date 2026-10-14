# web

Next.js app for the Quiet Check storefront and shopping agent (D5, `/`) and the merchant console with its review copilot
(D6, `/console`). Its server code calls the backend at `BACKEND_URL` (default `http://127.0.0.1:8000`); the browser only
calls this app's `/api/*` routes (see `../docs/contracts.md`, "Storefront routes" and "Console routes", and ADRs 0009
and 0010). It reads the repo-root `.env`.

Run it from the repo root with `make dev` (it needs the backend and members), or here with `pnpm dev`.
`pnpm lint` and `pnpm typecheck` run in CI.

The LLM is created in one place, `lib/model.ts`, so the provider can be swapped by env var. The shopping agent's prompt
is `../prompts/shopping_agent.md` and the copilot's `../prompts/review_copilot.md`; the personas come from
`../backend/app/personas.json`.
