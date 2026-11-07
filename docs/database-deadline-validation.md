# DB deadline regression validation

Validated locally on 2026-11-07 against the pinned Psycopg 3.3.8 and a disposable TLS PostgreSQL 17 container. No cloud DB, production image or running on-prem relay was changed.

## Deadline and cleanup

The opaque TCP relay forwards the real TLS handshake and authentication, then drops traffic in both directions, including new cancellation connections. It does not terminate TLS. Connection creation, query completion and commit failure are tested separately. The original `8a6309c` implementation took **16.006 seconds** for the same post-connect packet loss, reproducing the review finding.

The fixed request budget includes semaphore waiting, connection setup, queries, commit and cleanup. The final 250 ms is reserved for cleanup. The owned connection is closed before cancelling the driver worker. A worker retains its permit until its cleanup finishes; outstanding workers remain owned by the Database instance and are also aborted on application shutdown.

| Scenario | Measured result |
|---|---|
| `POST /db/check`, packet loss after TLS authentication | 503 in 5.763 s |
| `GET /db/products`, packet loss after TLS authentication | 503 in 5.757 s |
| Packet loss before commit | unavailable in 5.755 s |
| Four active requests and a fifth queued request | all unavailable in 5.753–5.756 s |
| `/ready`, 1.9 s cache probe alongside DB packet loss | 503 in 5.799 s |
| Caller cancellation | CancelledError propagated within 0.5 s; connection closed |
| Three cancelled connection attempts | relay TCP sockets and worker ownership cleared |
| Connection-slot saturation | queued request expires without opening a connection |

After each fault, the worker/connection is reclaimed, four permits are available, and a real verified query succeeds. `/health` remains 200. The timing values are one local measurement, not a deployment SLA.

## Reproduction and results

- `bash scripts/test-database.sh`: **18 tests passed**. Includes existing wrong-password, wrong-CA, wrong-hostname and server-enforced read-only INSERT rejection.
- `bash scripts/test-cache.sh`: **20 passed, 11 DB tests skipped** because this command creates only Redis. All 11 skipped DB cases passed in the PostgreSQL command above.
- Python compilation, shell syntax and `git diff --check` passed.

The PostgreSQL fixture waits for `pg_isready -h 127.0.0.1 -p 5432 -t 1`, not the temporary initialization socket. A monotonic deadline includes Docker subprocess timeouts. Socket-only initialization, delayed readiness, container exit, Docker timeout and overall timeout are regression tested; failure exits before the integration-test command.

This repository has no GitHub Actions workflow; these are local checks. Deployment requires a separately built, source-bound image and runtime verification.
