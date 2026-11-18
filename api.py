from __future__ import annotations

import os

from fastapi import APIRouter, FastAPI
from fastapi.middleware.cors import CORSMiddleware

from aftersales_service.routes import router as aftersales_router
from auth import router as auth_router


# The V1 knowledge Q&A routes live in `v1_api` and are mounted only when
# ENABLE_V1_ROUTES is true (local V1 development); by default they do not exist
# (404, not in OpenAPI) and `v1_api` - with the V1 SQLite store, RAG and Ollama
# client behind it - is never imported.
V1_ROUTES_ENV = "ENABLE_V1_ROUTES"
#: Always mounted: the process health check (read-only, no login).
public_router = APIRouter()


def v1_routes_enabled() -> bool:
    return os.environ.get(V1_ROUTES_ENV, "").strip().lower() in ("1", "true", "yes", "on")


@public_router.get("/api/health")
def health() -> dict:
    """Process liveness only: no model, database or knowledge-base state."""
    return {"status": "ok"}


def create_app(*, v1_routes: bool | None = None) -> FastAPI:
    """The API: login, the after-sales product, the health check, and V1 only if enabled.

    `v1_routes`: None reads ENABLE_V1_ROUTES (default off); tests of the V1
    routes pass True explicitly.
    """
    application = FastAPI(title="GroundedAgent After-Sales API", version="2.0.0")
    application.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    # GroundedAgent V2 M0: the after-sales product runtime (/api/aftersales/*),
    # behind JWT login (/auth/login; JWT_SECRET, see auth.py).
    application.include_router(auth_router)
    application.include_router(aftersales_router)
    application.include_router(public_router)
    if v1_routes_enabled() if v1_routes is None else v1_routes:
        from v1_api import v1_router

        application.include_router(v1_router)
    return application


app = create_app()
