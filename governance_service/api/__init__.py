"""API route registration."""

from fastapi import APIRouter

from . import health, pool

api_router = APIRouter()
api_router.include_router(health.router, tags=["health"])
api_router.include_router(pool.router, tags=["pool"])
