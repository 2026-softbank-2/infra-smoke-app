"""Bounded, read-only checks of an existing PostgreSQL database over verified TLS."""
import asyncio
from dataclasses import dataclass
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import time

import psycopg


@dataclass
class _Operation:
    connection: psycopg.AsyncConnection | None = None
    aborted: bool = False


class Database:
    REQUEST_TIMEOUT = 6.0
    CLEANUP_RESERVE = 0.25

    def __init__(self, settings: dict | None, catalog: bool = False):
        self.settings = settings
        self.catalog = catalog
        self.slots = asyncio.Semaphore(4)
        # Keep ownership if a driver ever fails to finish within the cleanup reserve.
        # Its semaphore permit stays with the worker until connection cleanup finishes.
        self._operations: dict[asyncio.Task, _Operation] = {}

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

    async def _execute(self, operation: _Operation, query: str, many: bool):
        async with self.slots:
            try:
                operation.connection = await psycopg.AsyncConnection.connect(**self.settings)
                if operation.aborted:
                    raise asyncio.CancelledError
                async with operation.connection as conn:
                    cursor = await conn.execute(query)
                    return await (cursor.fetchall() if many else cursor.fetchone())
            finally:
                if operation.connection is not None:
                    await operation.connection.close()

    def _finished(self, worker: asyncio.Task) -> None:
        self._operations.pop(worker, None)
        if not worker.cancelled():
            worker.exception()  # Observe failures even if the HTTP caller has gone away.

    async def _abort(self, worker: asyncio.Task, operation: _Operation) -> None:
        operation.aborted = True
        try:
            if operation.connection is not None:
                # No pool is used. Psycopg close() finishes the local libpq socket
                # without network I/O. Close BEFORE cancel so wait() doesn't start
                # the driver's extra 5s cancel + 5s result-drain protocol.
                await operation.connection.close()
        finally:
            if not worker.done():
                worker.cancel()

    async def _query(self, query: str, *, many: bool = False):
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.REQUEST_TIMEOUT
        operation = _Operation()
        worker = asyncio.create_task(self._execute(operation, query, many), name="database-query")
        self._operations[worker] = operation
        worker.add_done_callback(self._finished)
        try:
            # wait() doesn't cancel the driver when the deadline/caller is cancelled.
            done, _ = await asyncio.wait({worker}, timeout=max(0, deadline - loop.time() - self.CLEANUP_RESERVE))
            if not done:
                raise TimeoutError
            return worker.result()
        finally:
            if not worker.done():
                await self._abort(worker, operation)
                await asyncio.wait({worker}, timeout=max(0, min(self.CLEANUP_RESERVE, deadline - loop.time())))

    async def close(self) -> None:
        workers = dict(self._operations)
        for worker, operation in workers.items():
            await self._abort(worker, operation)
        if workers:
            await asyncio.wait(workers, timeout=self.CLEANUP_RESERVE)

    async def check(self) -> dict:
        checks = dict.fromkeys(("query", "tls", "read_only"), False)
        if self.settings is None:
            return {"status": "unconfigured", "checks": checks}
        start = time.monotonic()
        try:
            row = await self._query(
                "SELECT 1, current_setting('transaction_read_only'), ssl "
                "FROM pg_stat_ssl WHERE pid = pg_backend_pid()"
            )
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
            rows = await self._query("SELECT id, name, price, stock FROM products ORDER BY id LIMIT 10", many=True)
            return {"status": "ok", "items": [
                dict(zip(("id", "name", "price", "stock"), row)) for row in rows
            ]}
        except (psycopg.Error, TimeoutError, OSError):
            return {"status": "unavailable"}
