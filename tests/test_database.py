import os
from pathlib import Path
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
import psycopg

from database import Database
from main import app


class DatabaseConfigurationTests(unittest.TestCase):
    def test_unconfigured_database_keeps_http_available(self):
        with patch.dict(os.environ, {}, clear=True), TestClient(app) as client:
            self.assertEqual(client.get("/health").status_code, 200)
            for method, path in (("GET", "/db/health"), ("POST", "/db/check"), ("GET", "/db/products")):
                response = client.request(method, path)
                self.assertEqual(response.status_code, 503)
                self.assertEqual(response.json()["status"], "unconfigured")
                self.assertEqual(response.headers["cache-control"], "no-store")

    def test_partial_and_unverified_configurations_are_rejected(self):
        for extra in ({}, {"DB_SSLMODE": "require"}, {"DB_SSLMODE": "disable"}):
            with self.subTest(extra=extra), patch.dict(os.environ, {"DB_HOST": "db.test", **extra}, clear=True):
                with self.assertRaises(ValueError):
                    Database.from_environment()


@unittest.skipUnless(os.getenv("TEST_DB_CA_FILE"), "Set TEST_DB_CA_FILE for real TLS PostgreSQL tests")
class LiveDatabaseTests(unittest.TestCase):
    def setUp(self):
        self.settings = {
            "DB_HOST": "db.hunter.test", "DB_HOSTADDR": "127.0.0.1",
            "DB_PORT": os.getenv("TEST_DB_PORT", "15432"), "DB_NAME": "shop", "DB_USER": "shop_app",
            "DB_PASSWORD": "local-smoke-db-test-only", "DB_CA_FILE": os.environ["TEST_DB_CA_FILE"],
            "DB_SAMPLE_CATALOG": "true",
        }

    def test_query_tls_readonly_and_real_catalog(self):
        with patch.dict(os.environ, self.settings, clear=True), TestClient(app) as client:
            checked = client.post("/db/check")
            self.assertEqual(checked.status_code, 200, checked.text)
            self.assertTrue(all(checked.json()["checks"].values()))
            result = client.get("/db/products")
            self.assertEqual(result.status_code, 200, result.text)
            self.assertEqual(result.json()["items"], [{"id": 1, "name": "test-product", "price": 1000, "stock": 3}])
            settings = Database.from_environment().settings
        # The role has INSERT permission; the connection's server-side read-only setting blocks it.
        with psycopg.connect(**settings) as connection:
            with self.assertRaises(psycopg.errors.ReadOnlySqlTransaction):
                connection.execute("INSERT INTO products VALUES (99, 'must-not-exist', 1, 1)")
            connection.rollback()
            self.assertEqual(connection.execute("SELECT count(*) FROM products").fetchone()[0], 1)

    def test_bad_credentials_or_certificate_never_claim_success(self):
        for extra in ({"DB_PASSWORD": "wrong-private-password"}, {"DB_HOST": "wrong.hunter.test"},
                      {"DB_CA_FILE": os.environ["TEST_DB_WRONG_CA_FILE"]}):
            with self.subTest(extra=extra), patch.dict(os.environ, {**self.settings, **extra}, clear=True), TestClient(app) as client:
                result = client.post("/db/check")
                self.assertEqual(result.status_code, 503)
                self.assertEqual(result.json()["status"], "unavailable")
                self.assertNotIn("wrong-private-password", result.text)
                self.assertNotIn("db.hunter.test", result.text)
                self.assertEqual(client.get("/health").status_code, 200)

    def test_catalog_is_opt_in_and_hostaddr_is_an_ip(self):
        with patch.dict(os.environ, {**self.settings, "DB_SAMPLE_CATALOG": "false"}, clear=True), TestClient(app) as client:
            self.assertEqual(client.post("/db/check").status_code, 200)
            self.assertEqual(client.get("/db/products").json()["status"], "unconfigured")
        with patch.dict(os.environ, {**self.settings, "DB_HOSTADDR": "untrusted.example"}, clear=True):
            with self.assertRaises(ValueError):
                Database.from_environment()

    def test_connection_loss_does_not_break_basic_http(self):
        with patch.dict(os.environ, {**self.settings, "DB_PORT": "1"}, clear=True), TestClient(app) as client:
            self.assertEqual(client.get("/health").status_code, 200)
            self.assertEqual(client.post("/db/check").status_code, 503)
        with patch.dict(os.environ, self.settings, clear=True), TestClient(app) as client:
            self.assertEqual(client.post("/db/check").status_code, 200)


if __name__ == "__main__":
    unittest.main()
