"""API route registration."""

from fastapi import APIRouter

from . import health, pool, rounds

api_router = APIRouter()
api_router.include_router(health.router, tags=["health"])
api_router.include_router(pool.router, tags=["pool"])
api_router.include_router(rounds.router, tags=["rounds"])
