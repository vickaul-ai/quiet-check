# web

Next.js app for the Quiet Check storefront (shopping agent, D5) and merchant console (D6). It calls the backend on
port 8000; see `../docs/contracts.md`. Run it from the repo root with `make dev`, or here with `pnpm dev`.

The LLM model is created in one place, `lib/model.ts` (added in D5), so the provider can be swapped by env var.
