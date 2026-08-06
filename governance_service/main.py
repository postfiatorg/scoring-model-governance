"""FastAPI application entry point."""

import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI

from governance_service.api import api_router
from governance_service.database import init_db_if_needed
from governance_service.services.scheduler import scheduler_loop


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application startup and shutdown lifecycle."""
    init_db_if_needed()
    scheduler_task = asyncio.create_task(scheduler_loop())
    yield
    scheduler_task.cancel()
    try:
        await scheduler_task
    except asyncio.CancelledError:
        pass


def create_app() -> FastAPI:
    """Application factory."""
    app = FastAPI(
        title="Model Governance Service",
        description="Scoring-model governance rounds and candidate pool maintenance for the PFT Ledger",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.include_router(api_router)
    return app


app = create_app()
