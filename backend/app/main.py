"""Quiet Check backend.

D2 added /risk and the in-process AP3 initiator; D3 /catalog, /payments and /webhooks; D4 the
/merchant risk agent and read endpoints. See docs/contracts.md.
"""

import asyncio
import hmac
import os
import re
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request

from app.catalog import router as catalog_router
from app.config import Settings
from app.consortium.initiator import Initiator
from app.db import make_engine
from app.errors import error_response, install_error_handlers
from app.health import router as health_router
from app.merchant import router as merchant_router
from app.payments import router as payments_router
from app.paypal import PayPalClient
from app.risk import router as risk_router
from app.sweep import sweep_forever
from app.webhooks import router as webhooks_router
from app.workflow import router as workflow_router

# Open even when BACKEND_API_TOKEN is set (D9): Render's health check, PayPal's webhooks (verified
# by signature) and the status-only endpoint the storefront polls (contracts.md).
PUBLIC_ROUTES = (
    ("GET", re.compile(r"/health")),
    ("POST", re.compile(r"/webhooks/paypal")),
    ("GET", re.compile(r"/merchant/orders/[^/]+/status")),
)


def is_public(method: str, path: str) -> bool:
    return any(method == m and pattern.fullmatch(path) for m, pattern in PUBLIC_ROUTES)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    if os.environ.get("RENDER") and not settings.api_token:
        # Render sets RENDER=true. Hosted, the money endpoints must never be open.
        raise RuntimeError("BACKEND_API_TOKEN must be set on Render")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.engine = make_engine(settings.database_url)
        app.state.paypal = PayPalClient(
            settings.paypal_base_url,
            settings.paypal_client_id,
            settings.paypal_client_secret,
            transport=settings.paypal_transport,
        )
        app.state.initiator = None
        if settings.start_initiator:
            initiator = Initiator(
                port=settings.initiator_port,
                key_path=settings.initiator_key_path,
                member_urls=settings.member_urls,
                host=settings.initiator_host,
                card_url=settings.initiator_url,
                private_key=settings.initiator_private_key,
            )
            await initiator.start()
            app.state.initiator = initiator
        sweeper = (
            asyncio.create_task(sweep_forever(app.state)) if settings.sweep_interval_s else None
        )
        try:
            yield
        finally:
            if sweeper is not None:
                sweeper.cancel()
            if app.state.initiator is not None:
                await app.state.initiator.stop()
            await app.state.paypal.aclose()
            app.state.engine.dispose()

    app = FastAPI(title="Quiet Check backend", version="0.1.0", lifespan=lifespan)
    app.state.settings = settings
    app.state.last_start = {}  # /merchant/start cooldown (app/workflow.py)
    install_error_handlers(app)

    if settings.api_token:
        expected = f"Bearer {settings.api_token}".encode()

        @app.middleware("http")
        async def require_token(request: Request, call_next):
            if not is_public(request.method, request.url.path):
                given = request.headers.get("authorization", "").encode()
                if not hmac.compare_digest(given, expected):
                    return error_response(401, "unauthorized", "Missing or wrong API token.")
            return await call_next(request)

    app.include_router(catalog_router)
    app.include_router(risk_router)
    app.include_router(payments_router)
    app.include_router(merchant_router)
    app.include_router(webhooks_router)
    app.include_router(workflow_router)
    app.include_router(health_router)

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    return app


app = create_app()
