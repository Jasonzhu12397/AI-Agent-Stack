#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
TMP_DIR="$(mktemp -d)"
NETWORK="agent-mtls-ci-$RANDOM"
UPSTREAM="agent-mtls-upstream-$RANDOM"
PROXY="agent-mtls-proxy-$RANDOM"
PORT="$((18000 + RANDOM % 10000))"

cleanup() {
  docker rm -f "$PROXY" "$UPSTREAM" >/dev/null 2>&1 || true
  docker network rm "$NETWORK" >/dev/null 2>&1 || true
  rm -rf "$TMP_DIR"
}
trap cleanup EXIT

openssl req -x509 -newkey rsa:2048 -nodes \
  -keyout "$TMP_DIR/ca.key" -out "$TMP_DIR/ca.crt" \
  -subj "/CN=Agent mTLS Integration CA" -days 1

openssl req -newkey rsa:2048 -nodes \
  -keyout "$TMP_DIR/server.key" -out "$TMP_DIR/server.csr" \
  -subj "/CN=localhost"
printf 'subjectAltName=DNS:localhost,IP:127.0.0.1\nextendedKeyUsage=serverAuth\n' > "$TMP_DIR/server.ext"
openssl x509 -req -in "$TMP_DIR/server.csr" -CA "$TMP_DIR/ca.crt" \
  -CAkey "$TMP_DIR/ca.key" -CAcreateserial -out "$TMP_DIR/server.crt" \
  -days 1 -extfile "$TMP_DIR/server.ext"
cat "$TMP_DIR/server.crt" "$TMP_DIR/server.key" > "$TMP_DIR/agent-ingress.pem"

openssl req -newkey rsa:2048 -nodes \
  -keyout "$TMP_DIR/client.key" -out "$TMP_DIR/client.csr" \
  -subj "/CN=agent-node-ci"
printf 'extendedKeyUsage=clientAuth\n' > "$TMP_DIR/client.ext"
openssl x509 -req -in "$TMP_DIR/client.csr" -CA "$TMP_DIR/ca.crt" \
  -CAkey "$TMP_DIR/ca.key" -CAcreateserial -out "$TMP_DIR/client.crt" \
  -days 1 -extfile "$TMP_DIR/client.ext"

cat > "$TMP_DIR/upstream.py" <<'PY'
import json
from http.server import BaseHTTPRequestHandler, HTTPServer

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = json.dumps({
            "path": self.path,
            "fingerprint": self.headers.get("X-Verified-Client-Cert-SHA256"),
            "forwarded_proto": self.headers.get("X-Forwarded-Proto"),
        }).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

HTTPServer(("0.0.0.0", 8000), Handler).serve_forever()
PY

docker network create "$NETWORK" >/dev/null
docker run -d --name "$UPSTREAM" --network "$NETWORK" --network-alias api \
  -v "$TMP_DIR/upstream.py:/tmp/upstream.py:ro" \
  python:3.12-alpine python /tmp/upstream.py >/dev/null
docker run -d --name "$PROXY" --network "$NETWORK" \
  -p "127.0.0.1:$PORT:8443" \
  -v "$ROOT/deploy/cloud-control-plane/agent-mtls/haproxy.cfg:/usr/local/etc/haproxy/haproxy.cfg:ro" \
  -v "$TMP_DIR/agent-ingress.pem:/run/secrets/agent-ingress.pem:ro" \
  -v "$TMP_DIR/ca.crt:/run/secrets/agent-client-ca.pem:ro" \
  haproxy:3.2-alpine >/dev/null

EXPECTED="$(openssl x509 -in "$TMP_DIR/client.crt" -outform DER | sha256sum | awk '{print $1}')"
RESPONSE=""
for attempt in $(seq 1 30); do
  if RESPONSE="$(curl --silent --show-error --fail \
    --cacert "$TMP_DIR/ca.crt" --cert "$TMP_DIR/client.crt" --key "$TMP_DIR/client.key" \
    -H 'X-Verified-Client-Cert-SHA256: forged-value' \
    "https://localhost:$PORT/api/v1/agent-runtime/heartbeat" 2>/dev/null)"; then
    break
  fi
  sleep 1
done
test -n "$RESPONSE"
printf '%s' "$RESPONSE" | python -c '
import json, sys
data = json.load(sys.stdin)
assert data["path"] == "/api/v1/agent-runtime/heartbeat", data
assert data["forwarded_proto"] == "https", data
expected = sys.argv[1]
assert data["fingerprint"] == expected, data
' "$EXPECTED"

if curl --silent --output /dev/null --write-out '%{http_code}' \
  --cacert "$TMP_DIR/ca.crt" \
  "https://localhost:$PORT/api/v1/agent-runtime/heartbeat" | grep -q '^200$'; then
  echo "The mTLS ingress accepted a client without a certificate." >&2
  exit 1
fi

STATUS="$(curl --silent --output /dev/null --write-out '%{http_code}' \
  --cacert "$TMP_DIR/ca.crt" --cert "$TMP_DIR/client.crt" --key "$TMP_DIR/client.key" \
  "https://localhost:$PORT/api/v1/tenants/")"
test "$STATUS" = "404"

echo "Agent mTLS proxy integration passed: handshake, DER SHA-256 identity, spoofed-header stripping, and path restriction."
