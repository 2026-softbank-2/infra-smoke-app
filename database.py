"""Bounded, read-only checks of an existing PostgreSQL database over verified TLS."""
import asyncio
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import time

import psycopg


class Database:
    def __init__(self, settings: dict | None, catalog: bool = False):
        self.settings = settings
        self.catalog = catalog
        self.slots = asyncio.Semaphore(4)

    @classmethod
    def from_environment(cls) -> "Database":
        host = os.getenv("DB_HOST", "").strip()
        if not host:
            return cls(None)
        if os.getenv("DB_SSLMODE", "verify-full") != "verify-full":
            raise ValueError("Configured databases require DB_SSLMODE=verify-full")
        required = ("DB_NAME", "DB_USER", "DB_PASSWORD", "DB_CA_FILE")
        if any(not os.getenv(key) for key in required):
            raise ValueError("DB_NAME, DB_USER, DB_PASSWORD and DB_CA_FILE are required")
        if not Path(os.environ["DB_CA_FILE"]).is_file():
            raise ValueError("DB_CA_FILE must be a mounted CA bundle")
        port = int(os.getenv("DB_PORT", "5432"))
        if not 1 <= port <= 65535:
            raise ValueError("DB_PORT must be between 1 and 65535")
        settings = dict(
            host=host, port=port, dbname=os.environ["DB_NAME"], user=os.environ["DB_USER"],
            password=os.environ["DB_PASSWORD"], sslmode="verify-full", sslrootcert=os.environ["DB_CA_FILE"],
            connect_timeout=3, application_name="hunter-infra-smoke",
            options="-c default_transaction_read_only=on -c statement_timeout=2000 -c lock_timeout=1000",
        )
        if address := os.getenv("DB_HOSTADDR"):
            settings["hostaddr"] = str(ipaddress.ip_address(address))
        return cls(settings, os.getenv("DB_SAMPLE_CATALOG", "false").lower() == "true")

    async def check(self) -> dict:
        checks = dict.fromkeys(("query", "tls", "read_only"), False)
        if self.settings is None:
            return {"status": "unconfigured", "checks": checks}
        start = time.monotonic()
        try:
            async with asyncio.timeout(6), self.slots:
                async with await psycopg.AsyncConnection.connect(**self.settings) as conn:
                    cursor = await conn.execute(
                        "SELECT 1, current_setting('transaction_read_only'), ssl "
                        "FROM pg_stat_ssl WHERE pid = pg_backend_pid()"
                    )
                    row = await cursor.fetchone()
                    if row is not None:
                        checks = {"query": row[0] == 1, "read_only": row[1] == "on", "tls": row[2] is True}
        except (psycopg.Error, TimeoutError, OSError):
            # Connection errors can contain private endpoints or credentials.
            return {"status": "unavailable", "checks": checks}
        return {"status": "ok" if all(checks.values()) else "failed", "checks": checks,
                "latency_ms": round((time.monotonic() - start) * 1000, 1)}

    def identity(self) -> str:
        """Bind to the TLS-verified upstream name and DB, independent of relay IP/port and credentials."""
        if self.settings is None:
            return "none"
        upstream = [self.settings["host"].lower().rstrip("."), self.settings["dbname"]]
        return hashlib.sha256(json.dumps(upstream, ensure_ascii=True, separators=(",", ":")).encode()).hexdigest()

    async def products(self) -> dict:
        if self.settings is None or not self.catalog:
            return {"status": "unconfigured"}
        try:
            async with asyncio.timeout(6), self.slots:
                async with await psycopg.AsyncConnection.connect(**self.settings) as conn:
                    cursor = await conn.execute("SELECT id, name, price, stock FROM products ORDER BY id LIMIT 10")
                    rows = await cursor.fetchall()
                    return {"status": "ok", "items": [
                        dict(zip(("id", "name", "price", "stock"), row)) for row in rows
                    ]}
        except (psycopg.Error, TimeoutError, OSError):
            return {"status": "unavailable"}
