"""Bounded checks of an independent, disposable cache using CACHE_* settings."""

import asyncio
import json
import os
import ssl
from uuid import uuid4

from redis.asyncio import Redis
from redis.asyncio.retry import Retry
from redis.backoff import NoBackoff
from redis.exceptions import RedisError


class Cache:
    def __init__(self, client: Redis | None, version: str):
        self.client = client
        self.prefix = f"infra-smoke:{version}"
        self.check_slots = asyncio.Semaphore(4)

    @classmethod
    def from_environment(cls) -> "Cache":
        host = os.getenv("CACHE_HOST", "").strip()
        version = os.getenv("APP_VERSION", "dev")
        if not host:
            return cls(None, version)
        if os.getenv("CACHE_TLS", "true").lower() != "true":
            raise ValueError("Configured caches require CACHE_TLS=true")
        password = os.getenv("CACHE_PASSWORD")
        if not password:
            raise ValueError("Configured caches require CACHE_PASSWORD")
        port = int(os.getenv("CACHE_PORT", "6379"))
        if not 1 <= port <= 65535:
            raise ValueError("CACHE_PORT must be between 1 and 65535")
        # Constructed during lifespan, with connections opened only on use.
        client = Redis(
            host=host, port=port, password=password,
            ssl=True, ssl_cert_reqs="required", ssl_check_hostname=True,
            ssl_ca_data=os.getenv("CACHE_CA_CERT") or None,
            ssl_min_version=ssl.TLSVersion.TLSv1_2,
            decode_responses=True, socket_connect_timeout=1, socket_timeout=1,
            max_connections=8, retry=Retry(NoBackoff(), 0),
        )
        return cls(client, version)

    async def close(self) -> None:
        if self.client is not None:
            await self.client.aclose()

    async def healthy(self) -> bool:
        if self.client is None:
            return False
        try:
            async with asyncio.timeout(2):
                return bool(await self.client.ping())
        except (RedisError, TimeoutError, OSError):
            return False

    async def sample(self, item_id: int) -> dict:
        # Identical fixtures in both clouds can reconstruct an empty cache.
        item = {"id": item_id, "name": f"sample-{item_id}", "price": 1000 * item_id}
        expected = json.dumps(item, sort_keys=True, separators=(",", ":"))
        key = f"{self.prefix}:item:{item_id}"
        status = "unconfigured"
        if self.client is not None:
            try:
                async with asyncio.timeout(2.5):
                    cached = await self.client.get(key)
                    if cached is None:
                        stored = await self.client.set(key, expected, ex=60)
                        status = "miss" if stored else "unavailable"
                    else:
                        status = "hit" if cached == expected else "invalid"
            except (RedisError, TimeoutError, OSError):
                status = "unavailable"
        return {"item": item, "cache": status, "source": "cache" if status == "hit" else "fixture"}

    async def check(self) -> dict:
        checks = dict.fromkeys(["ping", "set", "get", "ttl", "expired"], False)
        if self.client is None:
            return {"status": "unconfigured", "checks": checks}
        # Fresh key/value: another request cannot make a failed write pass.
        token = uuid4().hex
        key = f"{self.prefix}:check:{token}"
        try:
            async with asyncio.timeout(5):
                async with self.check_slots:
                    checks["ping"] = bool(await self.client.ping())
                    checks["set"] = bool(await self.client.set(key, token, ex=1, nx=True))
                    checks["get"] = await self.client.get(key) == token
                    ttl = await self.client.pttl(key)
                    checks["ttl"] = 0 < ttl <= 1000
                    if not all(checks[name] for name in ("ping", "set", "get", "ttl")):
                        return {"status": "failed", "checks": checks}
                    await asyncio.sleep(ttl / 1000 + 0.05)
                    checks["expired"] = await self.client.get(key) is None
        except (RedisError, TimeoutError, OSError):
            # Error strings can contain credentials/endpoints; never expose them.
            return {"status": "unavailable", "checks": checks}
        return {"status": "ok" if all(checks.values()) else "failed", "checks": checks}
