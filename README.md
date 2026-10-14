# Quiet Check

A merchant agent privately checks an AI-agent purchase against other merchants' fraud lists (AP3 private set
intersection), then uses PayPal to capture, hold or decline the payment. No merchant ever shares its list.

Built for the PayPal AI Hackathon (2026). Status: the AP3 consortium check (D2), the PayPal payments service (D3), the merchant risk agent (D4) and the demo personas (D7) work; the storefront and console land through November 2026.

## Quickstart

Needs [uv](https://docs.astral.sh/uv/), [pnpm](https://pnpm.io/) 10, Node 22 and a PayPal sandbox REST app.

```sh
cp .env.example .env      # fill in PayPal sandbox and Anthropic keys
make install              # uv sync (backend) + pnpm install (web)
make sandbox              # checks the PayPal sandbox credentials can fetch an OAuth token
make seed                 # demo member lists (needs PERSONA_*_EMAIL in .env)
make dev                  # backend on :8000, web on :3000
```

## Commands

| Command | Does |
| --- | --- |
| `make dev` | Runs the backend (`:8000`, with the AP3 initiator on `:8100`), Members A-C (`:8101-8103`) and the web app (`:3000`). Members need list files in `backend/var/members/` (`make seed`) |
| `make seed` | Writes the three demo member lists (~300 synthetic entries each plus the persona seeds). Idempotent |
| `make dev-member-A` (`-B`, `-C`) | Runs one consortium member |
| `make check` | What CI runs: ruff, pytest, the log scan for list contents, eslint, type-check |
| `cd backend && uv run pytest -m slow -s` | Times `/risk/check` with ~300-entry member lists (target about 3 s) |
| `make sandbox` | `uv run scripts/check_sandbox.py` |
| `make approve-orders` | Creates sandbox orders, prints approval links, saves approved orders for live runs (needs `ngrok http 8000`, `make dev-backend` stopped) |
| `make test-live` | `pytest -m live`: the risk agent on 2 saved orders (clean -> capture; hold -> reviewer void), waiting for verified webhooks |
| `make gate-a END=capture` | Gate A on 1 saved order: the risk agent -> capture (or `END=void`: hold -> reviewer void) |
| `make live-personas` | Ana, Ben and Cy through the sandbox: approve 3 printed links as the 3 persona buyers; the webhook alone must give captured, held and blocked. `scripts/live_personas.py --persona ben` reruns a subset. Log out of sandbox PayPal between buyers: the checkout reuses the last login, even across incognito windows |

## Environment variables

Listed with defaults in `.env.example` and described in [docs/contracts.md](docs/contracts.md#environment-variables).

## Read more

- [docs/brief.md](docs/brief.md): the demo story, personas, decision policy, stack and scope
- [docs/contracts.md](docs/contracts.md): HTTP endpoints, webhooks, tables, normalisation, errors, idempotency
- [docs/PRODUCT.md](docs/PRODUCT.md) and [docs/ENGINEERING.md](docs/ENGINEERING.md)
- [docs/decisions/](docs/decisions/): architecture decision records

## Licence

Apache-2.0. See [LICENSE](LICENSE).
