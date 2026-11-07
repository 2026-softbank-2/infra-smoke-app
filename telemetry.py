"""Bounded Prometheus HTTP histograms shared with Hunter's canary metric contract."""
import asyncio
import hmac
import os
import re
from time import perf_counter

from prometheus_client import CollectorRegistry, Histogram, Info, generate_latest, CONTENT_TYPE_LATEST
from starlette.responses import Response


class Telemetry:
    def __init__(self, database_identity="none"):
        self.workload = os.getenv("APP_WORKLOAD", "smoke")
        if not re.fullmatch(r"[a-zA-Z][a-zA-Z0-9_-]{0,62}", self.workload):
            raise ValueError("APP_WORKLOAD must be a bounded identifier")
        self.token = os.getenv("METRICS_BEARER_TOKEN", "")
        self.registry = CollectorRegistry()
        version = os.getenv("APP_VERSION", "dev")
        binding = os.getenv("DB_BINDING", "unverified") if os.getenv("DB_HOST", "").strip() else "none"
        if not re.fullmatch(r"[A-Za-z0-9._:+-]{1,128}", version) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,62}", binding):
            raise ValueError("Runtime version and database binding must be bounded identifiers")
        self.metadata = {"workload": self.workload, "version": version, "database_binding": binding,
                         "database_identity": database_identity}
        self.runtime = Info("hunter_runtime", "Declared runtime artifact and database binding", registry=self.registry)
        self.runtime.info(self.metadata)
        self.requests = Histogram(
            "http_server_requests_seconds", "Completed HTTP request duration in seconds",
            ("method", "uri", "status", "workload"), registry=self.registry,
            buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2, 3, 5, 10),
        )

    def response(self, authorization: str) -> Response:
        if self.token and not hmac.compare_digest(authorization.encode(), ("Bearer " + self.token).encode()):
            return Response(status_code=401, headers={"Cache-Control": "no-store"})
        return Response(generate_latest(self.registry), media_type=CONTENT_TYPE_LATEST,
                        headers={"Cache-Control": "no-store"})


class MetricsMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        began = perf_counter()
        status = 500

        async def observed_send(message):
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, observed_send)
        except asyncio.CancelledError:
            status = 499
            raise
        finally:
            telemetry = getattr(scope["app"].state, "telemetry", None)
            if telemetry is not None:
                # Route templates only: never query strings, product IDs, headers or unknown raw paths.
                route = getattr(scope.get("route"), "path", "unmatched")
                method = scope.get("method", "OTHER")
                if method not in {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}:
                    method = "OTHER"
                telemetry.requests.labels(method, route, str(status), telemetry.workload).observe(perf_counter() - began)
