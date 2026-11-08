"""Minimal HTTP target for Hunter's ECS and Cloud Run infrastructure."""

import asyncio
import os
from contextlib import asynccontextmanager
from pathlib import Path as FilePath
from typing import Annotated
from uuid import uuid4

import uvicorn
from fastapi import FastAPI, Path, Request, Response
from fastapi.responses import HTMLResponse

from cache import Cache
from database import Database
from telemetry import MetricsMiddleware, Telemetry


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
dashboard_html = FilePath(__file__).with_name("dashboard.html").read_text()

@asynccontextmanager
async def lifespan(application: FastAPI):
    application.state.cache = Cache.from_environment()
    application.state.database = Database.from_environment()
    application.state.telemetry = Telemetry(application.state.database.identity())
    try:
        yield
    finally:
        await application.state.database.close()
        await application.state.cache.close()


app = FastAPI(
    title="infra-smoke-app",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
    lifespan=lifespan,
)
app.add_middleware(MetricsMiddleware)


@app.middleware("http")
async def no_store(request: Request, call_next):
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/health")
async def health(response: Response) -> dict[str, str]:
    response.headers["Cache-Control"] = "no-store"
    return {"status": "ok", "env": environment}


@app.get("/metrics")
async def metrics(request: Request) -> Response:
    return request.app.state.telemetry.response(request.headers.get("authorization", ""))


@app.get("/ready")
async def ready(request: Request, response: Response) -> dict:
    """Traffic eligibility requires the configured DB and local cache, unlike process liveness."""
    database = request.app.state.database
    cache_ready, db_result = await asyncio.gather(request.app.state.cache.healthy(), database.check())
    db_ready = database.settings is None or db_result["status"] == "ok"
    response.status_code = 200 if cache_ready and db_ready else 503
    return {"status": "ok" if response.status_code == 200 else "unavailable", "env": environment,
            "checks": {"cache": cache_ready, "database": db_ready}}


@app.get("/runtime")
async def runtime(request: Request) -> dict:
    """Non-secret declared identity; /ready separately verifies the configured dependencies."""
    return {"env": environment, **request.app.state.telemetry.metadata}


@app.get("/", response_model=None)
async def index(request: Request, response: Response):
    response.headers["Cache-Control"] = "no-store"
    response.headers["Vary"] = "Accept"
    if "text/html" in request.headers.get("accept", ""):
        return HTMLResponse(dashboard_html, headers={"Vary": "Accept"})
    return {
        "service": "infra-smoke-app",
        "env": environment,
        "version": version,
        "instance_id": instance_id,
    }


@app.get("/api/status")
async def dashboard_status(request: Request) -> dict:
    """One response binds all checks to the same instance behind multi-cloud routing."""
    cache = request.app.state.cache
    database = request.app.state.database
    cache_ready, db = await asyncio.gather(cache.healthy(), database.check())
    cache_status = "unconfigured" if cache.client is None else "ok" if cache_ready else "unavailable"
    return {"service": "infra-smoke-app", "env": environment, "instance_id": instance_id,
            **request.app.state.telemetry.metadata, "application": {"status": "ok"},
            "cache": {"status": cache_status, "checks": {"ping": cache_ready}}, "database": db}


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


@app.get("/db/health")
@app.post("/db/check")
async def database_check(request: Request, response: Response) -> dict:
    result = await request.app.state.database.check()
    response.status_code = 200 if result["status"] == "ok" else 503
    return {"env": environment, "version": version, **result}


@app.get("/db/products")
async def database_products(request: Request, response: Response) -> dict:
    result = await request.app.state.database.products()
    response.status_code = 200 if result["status"] == "ok" else 503
    return {"env": environment, "version": version, **result}


if __name__ == "__main__":
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=int(os.getenv("PORT", "8080")),
        proxy_headers=False,
        timeout_graceful_shutdown=8,
    )
