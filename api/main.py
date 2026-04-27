"""FastAPI entry point — API + static SvelteKit build."""

from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from api.routers import (
    adapters, base_models, data, datasets, delivery,
    health, models, playground, projects, rag, runs, stream,
)

app = FastAPI(title="MLOps Factory API", version="1.0.0", docs_url="/api/docs", redoc_url=None)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://localhost:7860"],
    allow_methods=["*"],
    allow_headers=["*"],
)

for r in (
    health.router, projects.router, base_models.router, datasets.router,
    runs.router, stream.router, models.router, delivery.router,
    data.router, rag.router, playground.router, adapters.router,
):
    app.include_router(r)


@app.on_event("startup")
async def _startup() -> None:
    from api.watchdog import watchdog_loop  # noqa: PLC0415
    asyncio.create_task(watchdog_loop(interval_seconds=60))


_STATIC = Path("dashboards")
if _STATIC.exists() and any(_STATIC.iterdir()):
    app.mount("/_app", StaticFiles(directory=str(_STATIC / "_app")), name="assets")

    @app.get("/{full_path:path}", include_in_schema=False)
    async def spa_fallback(full_path: str) -> FileResponse:
        static_file = _STATIC / full_path
        if static_file.is_file():
            return FileResponse(static_file)
        return FileResponse(_STATIC / "index.html")
