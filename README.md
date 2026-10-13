# Quiet Check

A merchant agent privately checks an AI-agent purchase against other merchants' fraud lists (AP3 private set
intersection), then uses PayPal to capture, hold or decline the payment. No merchant ever shares its list.

Built for the PayPal AI Hackathon (2026). Status: scaffold only (D1); features land through November 2026.

## Quickstart

Needs [uv](https://docs.astral.sh/uv/), [pnpm](https://pnpm.io/) 10, Node 22 and a PayPal sandbox REST app.

```sh
cp .env.example .env      # fill in PayPal sandbox and Anthropic keys
make install              # uv sync (backend) + pnpm install (web)
make sandbox              # checks the PayPal sandbox credentials can fetch an OAuth token
make dev                  # backend on :8000, web on :3000
```

## Commands

| Command | Does |
| --- | --- |
| `make dev` | Runs the FastAPI backend (`:8000`, `/health`) and the Next.js app (`:3000`) together |
| `make check` | What CI runs: ruff, pytest, eslint, type-check |
| `make sandbox` | `uv run scripts/check_sandbox.py` |

## Environment variables

Listed with defaults in `.env.example` and described in [docs/contracts.md](docs/contracts.md#environment-variables).

## Read more

- [docs/brief.md](docs/brief.md): the demo story, personas, decision policy, stack and scope
- [docs/contracts.md](docs/contracts.md): HTTP endpoints, webhooks, tables, normalisation, errors, idempotency
- [docs/PRODUCT.md](docs/PRODUCT.md) and [docs/ENGINEERING.md](docs/ENGINEERING.md)
- [docs/decisions/](docs/decisions/): architecture decision records

## Licence

Apache-2.0. See [LICENSE](LICENSE).
