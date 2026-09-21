from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.health import router as health_router
from app.core.config import Settings, get_settings
from app.core.errors import install_error_handlers
from app.core.logging import configure_logging
from app.core.resources import create_resources


def create_app(settings: Settings | None = None) -> FastAPI:
    resolved_settings = settings or get_settings()
    configure_logging(resolved_settings.log_level)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.settings = resolved_settings
        app.state.resources = create_resources(resolved_settings)
        try:
            yield
        finally:
            await app.state.resources.close()

    app = FastAPI(
        title="firefly-gen",
        version="0.1.0",
        lifespan=lifespan,
    )
    install_error_handlers(app)
    app.include_router(health_router)
    return app


app = create_app()
