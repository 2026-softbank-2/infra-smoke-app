"""The fixture must not admit Postgres's temporary socket-only init server."""
import importlib.util
from pathlib import Path
import subprocess
from types import SimpleNamespace
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location(
    "postgres_fixture", Path(__file__).resolve().parents[1] / "scripts/wait-for-postgres.py")
fixture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixture)


class DatabaseFixtureTests(unittest.TestCase):
    def setUp(self):
        self.now = 0
        self.probes = 0
        self.ready_at = 2
        self.running = True
        self.calls = []
        self.addCleanup(patch.stopall)
        patch.object(fixture.time, "monotonic", side_effect=lambda: self.now).start()
        patch.object(fixture.time, "sleep", side_effect=self.sleep).start()
        patch.object(fixture.subprocess, "run", side_effect=self.run_docker).start()

    def sleep(self, seconds):
        self.now += seconds

    def run_docker(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        if argv[1] == "inspect":
            return SimpleNamespace(stdout="true\n" if self.running else "false\n", returncode=0)
        if argv[1] == "logs":
            return SimpleNamespace(returncode=0)
        self.probes += 1
        # The init server always answers Unix sockets but exposes no TCP listener.
        tcp = "-h" in argv and argv[argv.index("-h") + 1] == "127.0.0.1"
        return SimpleNamespace(returncode=0 if not tcp or self.now >= self.ready_at else 1)

    def test_socket_only_init_is_rejected_until_final_tcp_server(self):
        fixture.wait_for_postgres("fixture", timeout=5)
        self.assertEqual(self.probes, 3)
        self.assertEqual(self.now, 2)
        probe = self.calls[-1][0]
        self.assertEqual(probe[probe.index("-p") + 1], "5432")
        self.assertEqual(probe[probe.index("-t") + 1], "1")

    def test_never_ready_exits_at_deadline(self):
        self.ready_at = float("inf")
        with self.assertRaises(TimeoutError):
            fixture.wait_for_postgres("fixture", timeout=2.5)
        self.assertEqual(self.now, 2.5)
        self.assertLessEqual(self.calls[-1][1]["timeout"], .5)

    def test_exited_init_container_fails_without_probing(self):
        self.running = False
        with self.assertRaisesRegex(RuntimeError, "exited"):
            fixture.wait_for_postgres("fixture")
        self.assertEqual(self.probes, 0)

    def test_docker_hang_is_bounded_and_main_returns_failure(self):
        with patch.object(fixture.subprocess, "run", side_effect=subprocess.TimeoutExpired("docker", 3)) as run:
            with patch.object(fixture.sys, "argv", ["wait-for-postgres.py", "fixture"]):
                self.assertEqual(fixture.main(), 1)
        self.assertEqual(run.call_args_list[0].kwargs["timeout"], 3)
        self.assertEqual(run.call_args_list[1].args[0][1], "logs")

    def test_readiness_timeout_returns_failure(self):
        with patch.object(fixture.sys, "argv", ["wait-for-postgres.py", "fixture"]):
            with patch.object(fixture, "wait_for_postgres", side_effect=TimeoutError):
                self.assertEqual(fixture.main(), 1)
