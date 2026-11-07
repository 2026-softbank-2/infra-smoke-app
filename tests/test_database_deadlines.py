"""Real TLS traffic loss must not turn the six-second budget into driver cancellation waits."""
import asyncio
from contextlib import suppress
import os
import time
import unittest
from unittest.mock import patch

import httpx
import psycopg

from database import Database
from main import app


class BlackholeRelay:
    """Opaque TCP forwarding: TLS stays end to end; dropping also affects cancel connections."""
    def __init__(self, upstream_port):
        self.upstream_port = upstream_port
        self.blocked = False
        self.tasks = set()
        self.writers = set()
        self.accepted = 0

    async def __aenter__(self):
        self.server = await asyncio.start_server(self.accept, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]
        return self

    def accept(self, reader, writer):
        self.accepted += 1
        task = asyncio.create_task(self.forward(reader, writer))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    async def forward(self, client_reader, client_writer):
        writers = [client_writer]
        pumps = []
        self.writers.add(client_writer)
        try:
            upstream_reader, upstream_writer = await asyncio.open_connection("127.0.0.1", self.upstream_port)
            writers.append(upstream_writer)
            self.writers.add(upstream_writer)

            async def pump(reader, writer):
                while data := await reader.read(65536):
                    if not self.blocked:
                        writer.write(data)
                        await writer.drain()

            pumps = [asyncio.create_task(pump(client_reader, upstream_writer)),
                     asyncio.create_task(pump(upstream_reader, client_writer))]
            await asyncio.wait(pumps, return_when=asyncio.FIRST_COMPLETED)
        except (OSError, asyncio.CancelledError):
            pass
        finally:
            for task in pumps:
                task.cancel()
            await asyncio.gather(*pumps, return_exceptions=True)
            for writer in writers:
                writer.close()
                with suppress(OSError):
                    await writer.wait_closed()
                self.writers.discard(writer)

    async def __aexit__(self, *_):
        self.server.close()
        await self.server.wait_closed()
        for writer in list(self.writers):
            writer.close()
        for task in list(self.tasks):
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)


@unittest.skipUnless(os.getenv("TEST_DB_CA_FILE"), "Set TEST_DB_CA_FILE for real TLS PostgreSQL tests")
class DatabaseDeadlineTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.env = {
            "DB_HOST": "db.hunter.test", "DB_HOSTADDR": "127.0.0.1",
            "DB_PORT": os.getenv("TEST_DB_PORT", "15432"), "DB_NAME": "shop", "DB_USER": "shop_app",
            "DB_PASSWORD": "local-smoke-db-test-only", "DB_CA_FILE": os.environ["TEST_DB_CA_FILE"],
            "DB_SAMPLE_CATALOG": "true",
        }
        self.connections = []
        self.connect = psycopg.AsyncConnection.connect
        self.relay = await self.enterAsyncContext(BlackholeRelay(int(self.env["DB_PORT"])))
        with patch.dict(os.environ, {**self.env, "DB_PORT": str(self.relay.port)}, clear=True):
            self.db = Database.from_environment()
        self.addAsyncCleanup(self.db.close)

    async def connect_then_drop(self, **settings):
        conn = await self.connect(**settings)
        self.connections.append(conn)
        self.relay.blocked = True
        return conn

    async def assert_recovered(self):
        self.relay.blocked = False
        result = await self.db.check()
        self.assertEqual(result["status"], "ok", result)
        self.assertTrue(all(conn.closed for conn in self.connections))
        # The worker, its connection and its concurrency permit are all reclaimed.
        await asyncio.sleep(0)
        self.assertFalse(self.db._operations)
        for _ in range(4):
            await asyncio.wait_for(self.db.slots.acquire(), timeout=.1)
        for _ in range(4):
            self.db.slots.release()

    async def test_tls_query_blackhole_finishes_inside_six_seconds_for_both_http_apis(self):
        with patch.dict(os.environ, {**self.env, "DB_PORT": str(self.relay.port)}, clear=True):
            async with app.router.lifespan_context(app):
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
                    for method, path in (("POST", "/db/check"), ("GET", "/db/products")):
                        with self.subTest(path=path):
                            with patch.object(psycopg.AsyncConnection, "connect", side_effect=self.connect_then_drop):
                                start = time.monotonic()
                                response = await client.request(method, path)
                                elapsed = time.monotonic() - start
                            print(f"TLS blackhole HTTP {path}: {elapsed:.3f}s")
                            self.assertEqual(response.status_code, 503)
                            self.assertEqual(response.json()["status"], "unavailable")
                            self.assertNotIn("db.hunter.test", response.text)
                            self.assertLess(elapsed, 6)
                            self.assertFalse(app.state.database._operations)
                            await self.assert_recovered()
                            self.assertEqual((await client.post("/db/check")).status_code, 200)

    async def test_commit_blackhole_is_included_in_same_deadline(self):
        commit = psycopg.AsyncConnection.commit

        async def drop_commit(conn):
            self.connections.append(conn)
            self.relay.blocked = True
            await commit(conn)

        with patch.object(psycopg.AsyncConnection, "commit", drop_commit):
            start = time.monotonic()
            result = await self.db.products()
            elapsed = time.monotonic() - start
        print(f"TLS blackhole commit: {elapsed:.3f}s")
        self.assertEqual(result["status"], "unavailable")
        self.assertLess(elapsed, 6)
        await self.assert_recovered()

    async def test_caller_cancel_propagates_and_closes_before_driver_cancel(self):
        connected = asyncio.Event()

        async def connect(**settings):
            conn = await self.connect_then_drop(**settings)
            connected.set()
            return conn

        with patch.object(psycopg.AsyncConnection, "connect", side_effect=connect):
            request = asyncio.create_task(self.db.check())
            await asyncio.wait_for(connected.wait(), timeout=2)
            await asyncio.sleep(.05)  # Query is now waiting for a packet the relay dropped.
            start = time.monotonic()
            request.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await request
        self.assertLess(time.monotonic() - start, .5)
        await self.assert_recovered()

    async def test_four_active_requests_and_one_queued_remain_bounded_then_recover(self):
        all_connected = asyncio.Event()

        async def connect(**settings):
            conn = await self.connect(**settings)
            self.connections.append(conn)
            if len(self.connections) == 4:
                self.relay.blocked = True
                all_connected.set()
            try:
                await all_connected.wait()
            except BaseException:
                await conn.close()
                raise
            return conn

        async def timed_check():
            start = time.monotonic()
            result = await self.db.check()
            return result, time.monotonic() - start

        with patch.object(psycopg.AsyncConnection, "connect", side_effect=connect):
            requests = [asyncio.create_task(timed_check()) for _ in range(4)]
            await asyncio.wait_for(all_connected.wait(), timeout=2)
            requests.append(asyncio.create_task(timed_check()))
            results = await asyncio.gather(*requests)
        self.assertTrue(all(result["status"] == "unavailable" for result, _ in results))
        self.assertTrue(all(elapsed < 6 for _, elapsed in results), results)
        print("TLS blackhole 4 active + 1 queued: " + ", ".join(f"{elapsed:.3f}s" for _, elapsed in results))
        await self.assert_recovered()

    async def test_saturated_slot_wait_is_inside_budget_without_opening_connection(self):
        for _ in range(4):
            await self.db.slots.acquire()
        try:
            with patch.object(self.db, "REQUEST_TIMEOUT", .1), patch.object(self.db, "CLEANUP_RESERVE", .02):
                with patch.object(psycopg.AsyncConnection, "connect") as connect:
                    start = time.monotonic()
                    result = await self.db.check()
                    self.assertLess(time.monotonic() - start, .15)
                    connect.assert_not_called()
                    self.assertEqual(result["status"], "unavailable")
        finally:
            for _ in range(4):
                self.db.slots.release()
        await self.assert_recovered()

    async def test_connect_blackhole_cancellation_reclaims_socket_and_worker(self):
        self.relay.blocked = True
        for _ in range(3):
            with patch.object(self.db, "REQUEST_TIMEOUT", .15), patch.object(self.db, "CLEANUP_RESERVE", .03):
                result = await self.db.check()
                self.assertEqual(result["status"], "unavailable")
        await asyncio.sleep(.05)
        self.assertFalse(self.relay.writers, "Cancelled connection attempts left TCP sockets open")
        await self.assert_recovered()

    async def test_http_ready_runs_cache_and_database_checks_concurrently(self):
        with patch.dict(os.environ, {**self.env, "DB_PORT": str(self.relay.port)}, clear=True):
            async with app.router.lifespan_context(app):
                async def slow_cache():
                    await asyncio.sleep(1.9)
                    return False
                with patch.object(app.state.cache, "healthy", side_effect=slow_cache), \
                        patch.object(psycopg.AsyncConnection, "connect", side_effect=self.connect_then_drop):
                    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
                        start = time.monotonic()
                        response = await client.get("/ready")
                        elapsed = time.monotonic() - start
                        self.assertEqual(response.status_code, 503)
                        self.assertEqual(response.json()["checks"], {"cache": False, "database": False})
                        self.assertLess(elapsed, 6)
                        self.assertEqual((await client.get("/health")).status_code, 200)
                self.assertTrue(all(conn.closed for conn in self.connections))
                self.assertFalse(app.state.database._operations)
        print(f"HTTP /ready with slow cache + DB blackhole: {elapsed:.3f}s")
