"""控制面入口：鉴权中间件 + 路由挂载 + 静态前端。"""
from __future__ import annotations

import os
import threading
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import api, proxy
from .manager import manager

TOKEN = os.environ.get("PANEL_TOKEN", "").strip()
STATIC_DIR = Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(_: FastAPI):
    threading.Thread(target=manager.autostart, daemon=True).start()
    yield
    manager.stop_all()


app = FastAPI(title="Phprun 容器控制面（PHP/Python/Node）", docs_url="/api/docs",
              openapi_url="/api/openapi.json", lifespan=lifespan)


@app.middleware("http")
async def auth(request: Request, call_next):
    path = request.url.path
    if TOKEN and path.startswith("/api") and not path.endswith(("/health",)):
        got = request.headers.get("x-token") or request.query_params.get("token") or ""
        if got != TOKEN:
            return JSONResponse({"detail": "令牌错误"}, status_code=401)
    return await call_next(request)


app.include_router(api.router)
app.include_router(proxy.router)


@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
