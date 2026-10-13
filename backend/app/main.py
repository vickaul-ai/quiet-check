"""Quiet Check backend.

Routers for /risk, /payments, /merchant and /webhooks arrive in D2-D4; see docs/contracts.md.
"""

from fastapi import FastAPI

app = FastAPI(title="Quiet Check backend", version="0.1.0")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
