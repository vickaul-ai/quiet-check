# Quiet Check

A merchant agent privately checks an AI-agent purchase against other merchants' fraud lists (AP3 private set
intersection), then uses PayPal to capture, hold or decline the payment. No merchant ever shares its list.

Built for the PayPal AI Hackathon (2026). Status: the AP3 consortium check works (D2); payments, agents and UI land through November 2026.

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
| `make dev` | Runs the backend (`:8000`, with the AP3 initiator on `:8100`), Members A-C (`:8101-8103`) and the web app (`:3000`). Members need list files in `backend/var/members/` (D7 seeds them) |
| `make dev-member-A` (`-B`, `-C`) | Runs one consortium member |
| `make check` | What CI runs: ruff, pytest, the log scan for list contents, eslint, type-check |
| `cd backend && uv run pytest -m slow -s` | Times `/risk/check` with ~300-entry member lists (target about 3 s) |
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
