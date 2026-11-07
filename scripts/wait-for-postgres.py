#!/usr/bin/env python3
"""Wait for the final TCP server, with a deadline covering Docker calls too."""
import subprocess
import sys
import time


def wait_for_postgres(container: str, timeout: float = 60) -> None:
    deadline = time.monotonic() + timeout
    while (remaining := deadline - time.monotonic()) > 0:
        state = subprocess.run(
            ["docker", "inspect", "--format", "{{.State.Running}}", container],
            capture_output=True, text=True, timeout=min(3, remaining), check=True,
        )
        if state.stdout.strip() != "true":
            raise RuntimeError("PostgreSQL container exited during initialization")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        probe = subprocess.run(
            ["docker", "exec", container, "pg_isready", "-h", "127.0.0.1", "-p", "5432",
             "-U", "postgres", "-d", "shop", "-t", "1"],
            capture_output=True, timeout=min(3, remaining),
        )
        if probe.returncode == 0 and time.monotonic() < deadline:
            return
        time.sleep(min(1, max(0, deadline - time.monotonic())))
    raise TimeoutError("PostgreSQL TCP readiness deadline exceeded")


def main() -> int:
    container = sys.argv[1]
    try:
        wait_for_postgres(container)
    except (RuntimeError, TimeoutError, subprocess.SubprocessError) as exc:
        print(f"Database fixture failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        try:
            subprocess.run(["docker", "logs", "--tail", "30", container], timeout=3, check=False)
        except subprocess.SubprocessError:
            print("Container diagnostics unavailable", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
