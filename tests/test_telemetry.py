import os
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from main import app
from database import Database


class TelemetryTests(unittest.TestCase):
    def test_database_identity_binds_the_verified_host_and_database_without_credentials(self):
        original = Database({"host": "DB.Example.test", "dbname": "shop", "password": "secret", "port": 5432})
        relay = Database({"host": "db.example.test", "dbname": "shop", "password": "rotated", "hostaddr": "127.0.0.1", "port": 15432})
        other = Database({"host": "db.example.test", "dbname": "another"})
        self.assertEqual(original.identity(), relay.identity())
        self.assertNotEqual(original.identity(), other.identity())
        self.assertRegex(original.identity(), r"^[a-f0-9]{64}$")

    def test_runtime_metadata_does_not_invent_a_database(self):
        with patch.dict(os.environ, {"APP_WORKLOAD": "another-app", "APP_VERSION": "sha-test"}, clear=True), TestClient(app) as client:
            value = client.get("/runtime").json()
            self.assertEqual(value["workload"], "another-app")
            self.assertEqual(value["database_binding"], "none")
            self.assertEqual(value["database_identity"], "none")
            self.assertEqual(value["version"], "sha-test")

    def test_histogram_uses_templates_and_workload_without_private_request_data(self):
        with patch.dict(os.environ, {"APP_WORKLOAD": "smoke"}, clear=True), TestClient(app) as client:
            client.get("/cache/items/1?token=must-never-be-a-metric", headers={"Authorization": "Bearer private-header"})
            client.get("/private-unknown-path")
            body = client.get("/metrics").text
            self.assertIn('http_server_requests_seconds_count', body)
            self.assertIn('uri="/cache/items/{item_id}"', body)
            self.assertIn('workload="smoke"', body)
            self.assertIn('uri="unmatched"', body)
            for private in ("must-never", "private-header", "private-unknown-path"):
                self.assertNotIn(private, body)

    def test_optional_metrics_token_is_enforced_without_affecting_http(self):
        with patch.dict(os.environ, {"METRICS_BEARER_TOKEN": "test-only"}, clear=True), TestClient(app) as client:
            self.assertEqual(client.get("/metrics").status_code, 401)
            self.assertEqual(client.get("/metrics", headers={"Authorization": "Bearer wrong"}).status_code, 401)
            self.assertEqual(client.get("/metrics", headers={"Authorization": "Bearer test-only"}).status_code, 200)
            self.assertEqual(client.get("/health").status_code, 200)

    def test_liveness_does_not_claim_dependency_readiness(self):
        with patch.dict(os.environ, {}, clear=True), TestClient(app) as client:
            self.assertEqual(client.get("/health").status_code, 200)
            self.assertEqual(client.get("/ready").status_code, 503)
            self.assertFalse(client.get("/ready").json()["checks"]["cache"])

    def test_each_lifespan_has_an_independent_registry(self):
        with patch.dict(os.environ, {}, clear=True):
            for _ in range(2):
                with TestClient(app) as client:
                    self.assertNotIn('http_server_requests_seconds_count{', client.get("/metrics").text)
