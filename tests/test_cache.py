import os
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch
from uuid import uuid4

from fastapi.testclient import TestClient
import redis

from cache import Cache
from main import app


class ConfigurationTests(unittest.TestCase):
    def test_unconfigured_cache_does_not_claim_success(self):
        with patch.dict(os.environ, {}, clear=True), TestClient(app) as client:
            self.assertEqual(client.get("/health").status_code, 200)
            self.assertEqual(client.get("/cache/health").status_code, 503)
            checked = client.post("/cache/check")
            self.assertEqual(checked.status_code, 503)
            self.assertEqual(checked.json()["status"], "unconfigured")
            item = client.get("/cache/items/1")
            self.assertEqual(item.json()["source"], "fixture")
            self.assertEqual(item.headers["Cache-Control"], "no-store")
            self.assertEqual(client.get("/cache/items/4").status_code, 422)

    def test_plaintext_and_missing_password_are_rejected(self):
        for extra in ({"CACHE_TLS": "false", "CACHE_PASSWORD": "test-only"}, {}):
            with self.subTest(extra=extra), patch.dict(os.environ, {"CACHE_HOST": "localhost", **extra}, clear=True):
                with self.assertRaises(ValueError):
                    Cache.from_environment()


@unittest.skipUnless(os.getenv("TEST_CACHE_CA_FILE"), "Set TEST_CACHE_CA_FILE for real TLS Redis tests")
class LiveRedisTests(unittest.TestCase):
    def setUp(self):
        self.version = "test-" + uuid4().hex
        self.settings = {
            "CACHE_HOST": "127.0.0.1",
            "CACHE_PORT": os.getenv("TEST_CACHE_PORT", "16379"),
            "CACHE_TLS": "true",
            "CACHE_PASSWORD": "local-smoke-test-only",
            "CACHE_CA_CERT": Path(os.environ["TEST_CACHE_CA_FILE"]).read_text(),
            "APP_VERSION": self.version,
        }
        self.raw = redis.Redis(
            host=self.settings["CACHE_HOST"], port=int(self.settings["CACHE_PORT"]),
            password=self.settings["CACHE_PASSWORD"], ssl=True,
            ssl_ca_data=self.settings["CACHE_CA_CERT"], ssl_check_hostname=True,
            decode_responses=True, socket_timeout=2, socket_connect_timeout=2,
        )
        self.assertTrue(self.raw.ping())

    def tearDown(self):
        self.raw.close()

    def test_live_checks_verify_write_value_and_actual_expiration(self):
        with patch.dict(os.environ, self.settings), TestClient(app) as client:
            checked = client.post("/cache/check")
            self.assertEqual(checked.status_code, 200, checked.text)
            self.assertEqual(checked.json()["checks"], dict.fromkeys(["ping", "set", "get", "ttl", "expired"], True))
            # An independent Redis client verifies no check key survived.
            self.assertEqual(list(self.raw.scan_iter(f"infra-smoke:{self.version}:check:*")), [])

    def test_miss_hit_and_corrupt_cache_fall_back_to_same_fixture(self):
        with patch.dict(os.environ, self.settings), TestClient(app) as client:
            first = client.get("/cache/items/1").json()
            second = client.get("/cache/items/1").json()
            self.assertEqual((first["cache"], second["cache"]), ("miss", "hit"))
            self.assertEqual(first["item"], second["item"])
            self.assertGreater(self.raw.ttl(f"infra-smoke:{self.version}:item:1"), 0)
            self.raw.set(f"infra-smoke:{self.version}:item:1", "untrusted-cached-content", ex=60)
            recovered = client.get("/cache/items/1")
            self.assertEqual(recovered.json()["cache"], "invalid")
            self.assertEqual(recovered.json()["item"], first["item"])
            self.assertNotIn("untrusted-cached-content", recovered.text)

    def test_wrong_password_is_unavailable_while_app_serves_fixtures(self):
        settings = {**self.settings, "CACHE_PASSWORD": "incorrect-test-only-password"}
        with patch.dict(os.environ, settings), TestClient(app) as client:
            self.assertEqual(client.get("/health").status_code, 200)
            self.assertEqual(client.get("/cache/health").status_code, 503)
            checked = client.post("/cache/check")
            self.assertEqual(checked.status_code, 503)
            self.assertNotIn(settings["CACHE_PASSWORD"], checked.text)
            item = client.get("/cache/items/1")
            self.assertEqual(item.status_code, 200)
            self.assertEqual(item.json()["cache"], "unavailable")

    def test_untrusted_certificate_and_wrong_hostname_are_rejected(self):
        for changed in ({"CACHE_CA_CERT": ""}, {"CACHE_HOST": "localhost"}):
            with self.subTest(changed=changed), patch.dict(os.environ, {**self.settings, **changed}), TestClient(app) as client:
                self.assertEqual(client.get("/cache/health").status_code, 503)
        # The certificate only covers 127.0.0.1, so localhost must fail identity validation.
        wrong_host = redis.Redis(
            host="localhost", port=int(self.settings["CACHE_PORT"]),
            password=self.settings["CACHE_PASSWORD"], ssl=True,
            ssl_ca_data=self.settings["CACHE_CA_CERT"], ssl_check_hostname=True,
            socket_timeout=1, socket_connect_timeout=1,
        )
        try:
            with self.assertRaisesRegex(redis.ConnectionError, "certificate verify failed"):
                wrong_host.ping()
        finally:
            wrong_host.close()

    @unittest.skipUnless(os.getenv("TEST_REDIS_CONTAINER"), "Requires an owned test container")
    def test_connection_loss_and_recovery_keep_app_available(self):
        name = os.environ["TEST_REDIS_CONTAINER"]
        self.assertTrue(name.startswith("hunter-smoke-redis-test-"))
        with patch.dict(os.environ, self.settings), TestClient(app) as client:
            self.assertEqual(client.get("/cache/health").status_code, 200)
            subprocess.run(["docker", "pause", name], check=True, capture_output=True)
            try:
                self.assertEqual(client.get("/health").status_code, 200)
                self.assertEqual(client.get("/cache/health").status_code, 503)
                item = client.get("/cache/items/1")
                self.assertEqual(item.status_code, 200)
                self.assertEqual(item.json()["cache"], "unavailable")
            finally:
                subprocess.run(["docker", "unpause", name], check=True, capture_output=True)
            self.assertEqual(client.get("/cache/health").status_code, 200)
            self.assertEqual(client.post("/cache/check").status_code, 200)


if __name__ == "__main__":
    unittest.main()
