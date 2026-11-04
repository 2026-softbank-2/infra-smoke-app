"""Minimal HTTP target for Hunter's ECS and Cloud Run infrastructure."""

import os
from contextlib import asynccontextmanager
from typing import Annotated
from uuid import uuid4

import uvicorn
from fastapi import FastAPI, Path, Request, Response

from cache import Cache


def deployment_environment() -> str:
    if override := os.getenv("APP_ENV"):
        return override
    if os.getenv("K_SERVICE"):
        return "gcp"
    if os.getenv("AWS_EXECUTION_ENV", "").startswith("AWS_ECS_"):
        return "aws"
    return "local"


environment = deployment_environment()
version = os.getenv("APP_VERSION", "dev")
instance_id = uuid4().hex

@asynccontextmanager
async def lifespan(application: FastAPI):
    application.state.cache = Cache.from_environment()
    try:
        yield
    finally:
        await application.state.cache.close()


app = FastAPI(
    title="infra-smoke-app",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
    lifespan=lifespan,
)


@app.middleware("http")
async def no_store(request: Request, call_next):
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/health")
async def health(response: Response) -> dict[str, str]:
    response.headers["Cache-Control"] = "no-store"
    return {"status": "ok", "env": environment}


@app.get("/")
async def index(response: Response) -> dict[str, str]:
    response.headers["Cache-Control"] = "no-store"
    return {
        "service": "infra-smoke-app",
        "env": environment,
        "version": version,
        "instance_id": instance_id,
    }


@app.get("/cache/health")
async def cache_health(request: Request, response: Response) -> dict:
    ready = await request.app.state.cache.healthy()
    response.status_code = 200 if ready else 503
    return {"status": "ok" if ready else "unavailable", "env": environment}


@app.post("/cache/check")
async def cache_check(request: Request, response: Response) -> dict:
    result = await request.app.state.cache.check()
    response.status_code = 200 if result["status"] == "ok" else 503
    return {"env": environment, "version": version, **result}


@app.get("/cache/items/{item_id}")
async def cached_item(request: Request, item_id: Annotated[int, Path(ge=1, le=3)]) -> dict:
    return {"env": environment, "version": version, **await request.app.state.cache.sample(item_id)}


if __name__ == "__main__":
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=int(os.getenv("PORT", "8080")),
        proxy_headers=False,
        timeout_graceful_shutdown=8,
    )
