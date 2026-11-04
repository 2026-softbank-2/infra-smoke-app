"""Minimal HTTP target for Hunter's ECS and Cloud Run infrastructure."""

import os
from uuid import uuid4

import uvicorn
from fastapi import FastAPI, Response


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

app = FastAPI(
    title="infra-smoke-app",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)


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


if __name__ == "__main__":
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=int(os.getenv("PORT", "8080")),
        proxy_headers=False,
        timeout_graceful_shutdown=8,
    )
