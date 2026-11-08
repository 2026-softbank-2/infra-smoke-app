import os
import unittest
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from main import app


class DashboardTests(unittest.TestCase):
    def test_browser_html_preserves_existing_json_clients(self):
        with patch.dict(os.environ, {}, clear=True), TestClient(app) as client:
            json = client.get("/")
            self.assertEqual(json.status_code, 200)
            self.assertEqual(json.json()["service"], "infra-smoke-app")
            html = client.get("/", headers={"Accept": "text/html"})
            self.assertEqual(html.status_code, 200)
            self.assertIn("text/html", html.headers["content-type"])
            self.assertIn("/api/status", html.text)
            self.assertEqual(html.headers["cache-control"], "no-store")
            self.assertEqual(html.headers["vary"], "Accept")
            self.assertEqual(json.headers["vary"], "Accept")

    def test_unconfigured_dependencies_are_not_reported_as_healthy(self):
        with patch.dict(os.environ, {"PRIVATE_PASSWORD": "must-not-appear"}, clear=True), TestClient(app) as client:
            response = client.get("/api/status")
            status = response.json()
            self.assertEqual(status["application"]["status"], "ok")
            self.assertEqual(status["cache"]["status"], "unconfigured")
            self.assertEqual(status["database"]["status"], "unconfigured")
            self.assertEqual(status["instance_id"], client.get("/").json()["instance_id"])
            self.assertNotIn("must-not-appear", response.text)

    def test_dependency_failure_keeps_aggregate_available_and_truthful(self):
        with patch.dict(os.environ, {}, clear=True), TestClient(app) as client:
            with patch.object(app.state.database, "check", new=AsyncMock(return_value={"status": "unavailable", "checks": {"query": False}})):
                response = client.get("/api/status")
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()["database"]["status"], "unavailable")
                self.assertEqual(response.json()["application"]["status"], "ok")
