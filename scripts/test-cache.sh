#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
umask 077
mkdir -p .validation
test_dir="$(mktemp -d "$PWD/.validation/cache-test.XXXXXX")"
container="hunter-smoke-redis-test-$$"
cleanup() { docker rm -f "$container" > /dev/null 2>&1 || true; }
trap cleanup EXIT
openssl req -x509 -newkey rsa:2048 -nodes -days 1 \
  -keyout "$test_dir/ca.key" -out "$test_dir/ca.crt" -subj /CN=Hunter-Smoke-Test-CA \
  -addext 'basicConstraints=critical,CA:TRUE' -addext 'keyUsage=critical,keyCertSign,cRLSign' \
  -addext 'subjectKeyIdentifier=hash' > "$test_dir/openssl.log" 2>&1
openssl req -new -newkey rsa:2048 -nodes -keyout "$test_dir/server.key" \
  -out "$test_dir/server.csr" -subj /CN=Hunter-Smoke-Test >> "$test_dir/openssl.log" 2>&1
cat > "$test_dir/server.ext" <<'EOF'
basicConstraints=critical,CA:FALSE
keyUsage=critical,digitalSignature,keyEncipherment
extendedKeyUsage=serverAuth
subjectAltName=IP:127.0.0.1,DNS:test-cache
subjectKeyIdentifier=hash
authorityKeyIdentifier=keyid,issuer
EOF
openssl x509 -req -in "$test_dir/server.csr" -CA "$test_dir/ca.crt" -CAkey "$test_dir/ca.key" \
  -CAcreateserial -days 1 -out "$test_dir/server.crt" -extfile "$test_dir/server.ext" >> "$test_dir/openssl.log" 2>&1
docker run -d --name "$container" --user 0:0 -p 127.0.0.1::6379 \
  --mount "type=bind,src=$test_dir,dst=/tls,readonly" \
  redis:7.2@sha256:0312984fbfeb2ff1d4382ecef45b50e7d65478a6a58cf12ce28d6de7a4142e45 \
  redis-server --port 0 --tls-port 6379 --tls-cert-file /tls/server.crt \
  --tls-key-file /tls/server.key --tls-ca-cert-file /tls/ca.crt --tls-auth-clients no \
  --requirepass local-smoke-test-only --save '' --appendonly no > "$test_dir/container-id"
export TEST_CACHE_PORT="$(docker port "$container" 6379/tcp | sed 's/.*://')"
export TEST_CACHE_CA_FILE="$test_dir/ca.crt"
export TEST_REDIS_CONTAINER="$container"
.venv/bin/python - <<'PY'
import os, socket, time
deadline = time.monotonic() + 15
while True:
    try:
        with socket.create_connection(('127.0.0.1', int(os.environ['TEST_CACHE_PORT'])), timeout=1):
            break
    except OSError:
        if time.monotonic() >= deadline:
            raise
        time.sleep(0.2)
PY
.venv/bin/python -m unittest discover -s tests -v
