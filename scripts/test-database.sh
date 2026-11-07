#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
work=$(mktemp -d)
container="hunter-smoke-db-test-$(date +%s)-$$"
cleanup() { docker rm -f "$container" >/dev/null 2>&1 || true; rm -rf "$work"; }
trap cleanup EXIT
openssl req -x509 -newkey rsa:2048 -nodes -days 1 -subj /CN=Hunter-test-CA \
  -keyout "$work/ca.key" -out "$work/ca.crt" >/dev/null 2>&1
openssl req -new -newkey rsa:2048 -nodes -subj /CN=db.hunter.test \
  -keyout "$work/server.key" -out "$work/server.csr" >/dev/null 2>&1
printf 'subjectAltName=DNS:db.hunter.test\nextendedKeyUsage=serverAuth\n' > "$work/server.ext"
openssl x509 -req -in "$work/server.csr" -CA "$work/ca.crt" -CAkey "$work/ca.key" \
  -CAcreateserial -days 1 -extfile "$work/server.ext" -out "$work/server.crt" >/dev/null 2>&1
openssl req -x509 -newkey rsa:2048 -nodes -days 1 -subj /CN=Untrusted-test-CA \
  -keyout "$work/wrong.key" -out "$work/wrong.crt" >/dev/null 2>&1
cat > "$work/init.sql" <<'SQL'
CREATE ROLE shop_app LOGIN PASSWORD 'local-smoke-db-test-only';
CREATE TABLE products (id bigint PRIMARY KEY, name text, price bigint, stock integer);
INSERT INTO products VALUES (1, 'test-product', 1000, 3);
GRANT SELECT, INSERT ON products TO shop_app;
SQL
docker run -d --name "$container" -p 127.0.0.1::5432 \
  -e POSTGRES_PASSWORD=local-test-owner-only -e POSTGRES_DB=shop \
  --mount "type=bind,src=$work,dst=/certs,readonly" \
  --mount "type=bind,src=$work/init.sql,dst=/docker-entrypoint-initdb.d/init.sql,readonly" \
  --entrypoint bash postgres:17 -ec \
  'cp /certs/server.key /certs/server.crt /tmp/; chown postgres:postgres /tmp/server.*; chmod 600 /tmp/server.key; exec docker-entrypoint.sh postgres -c ssl=on -c ssl_cert_file=/tmp/server.crt -c ssl_key_file=/tmp/server.key' >/dev/null
for attempt in $(seq 1 60); do
  if docker exec "$container" pg_isready -U postgres -d shop >/dev/null 2>&1; then break; fi
  sleep 1
done
port=$(docker port "$container" 5432/tcp | sed 's/.*://')
TEST_DB_CA_FILE="$work/ca.crt" TEST_DB_WRONG_CA_FILE="$work/wrong.crt" TEST_DB_PORT="$port" \
  .venv/bin/python -m unittest discover -s tests -p test_database.py -v
