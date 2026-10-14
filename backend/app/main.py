"""Quiet Check backend.

D2 adds /risk and the in-process AP3 initiator; /payments, /merchant and /webhooks arrive in
D3-D4. See docs/contracts.md.
"""

from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.config import Settings
from app.consortium.initiator import Initiator
from app.db import make_engine
from app.errors import install_error_handlers
from app.risk import router as risk_router


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.engine = make_engine(settings.database_url)
        app.state.initiator = None
        if settings.start_initiator:
            initiator = Initiator(
                port=settings.initiator_port,
                key_path=settings.initiator_key_path,
                member_urls=settings.member_urls,
            )
            await initiator.start()
            app.state.initiator = initiator
        try:
            yield
        finally:
            if app.state.initiator is not None:
                await app.state.initiator.stop()
            app.state.engine.dispose()

    app = FastAPI(title="Quiet Check backend", version="0.1.0", lifespan=lifespan)
    app.state.settings = settings
    install_error_handlers(app)
    app.include_router(risk_router)

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    return app


app = create_app()
