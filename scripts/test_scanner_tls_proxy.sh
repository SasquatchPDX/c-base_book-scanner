#!/usr/bin/env bash
# Live check of deploy/nginx/serif-lan-tls.conf and the plain-HTTP guard in
# deploy/nginx/book-scanner.conf (issue #132).
#
# Starts the pinned nginx image three times with host networking, so nginx sees
# the client as 127.0.0.1:
#   * allowed: book-scanner.conf + serif-lan-tls.conf with `allow 127.0.0.1;`
#              in place of the first commented example range;
#   * ranges:  serif-lan-tls.conf with the commented example ranges enabled
#              (RFC 5737 documentation networks, 127.0.0.1 is outside them);
#   * shipped: serif-lan-tls.conf exactly as shipped (fail closed: deny all).
# Only listen ports and the upstream address are rewritten. The certificate
# comes from the internal-CA procedure in docs/operations.md, and a small
# Python HTTP server stands in for Uvicorn.
#
# Asserts: TLS 1.3 works and verifies against the CA and the host name; a
# TLS 1.2-only client cannot complete a handshake; a source outside the allowed
# ranges gets 403, and so does everyone with the unedited file; /api/scanner/
# accepts bodies above nginx's 1 MiB default and rejects bodies above 64 MiB
# with 413; plain HTTP (port 80 server and book-scanner.conf's 8080 listener)
# never serves /api/scanner/.
#
# Linux only (host networking). Needs docker, openssl, curl and python3.
set -Eeuo pipefail

project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
serif_config="${project_root}/deploy/nginx/serif-lan-tls.conf"
plain_config="${project_root}/deploy/nginx/book-scanner.conf"
nginx_image="nginx:1.31.3-alpine@sha256:4a73073bd557c65b759505da037898b61f1be6cbcc3c2c3aeac22d2a470c1752"
host_name="serif.lan.example"
port_base="${BOOK_SCANNER_SCANNER_PROXY_PORT_BASE:-18440}"
upstream_port="${BOOK_SCANNER_SCANNER_PROXY_UPSTREAM_PORT:-18083}"
# Per role: HTTPS = base+N, HTTP (port 80 server) = base+10+N.
allowed_https=$((port_base + 0))
ranges_https=$((port_base + 1))
shipped_https=$((port_base + 2))
allowed_http=$((port_base + 10))
ranges_http=$((port_base + 11))
shipped_http=$((port_base + 12))
legacy_http=$((port_base + 20)) # book-scanner.conf's `listen 8080`

work_dir="$(mktemp -d -t book-scanner-tls-proxy.XXXXXX)"
container_prefix="book-scanner-tls-proxy-$$"
roles=(allowed ranges shipped)
upstream_pid=""

cleanup() {
  local status=$?
  trap - EXIT
  for role in "${roles[@]}"; do
    if ((status != 0)); then
      docker logs "${container_prefix}-${role}" 2>&1 | tail -50 || true
    fi
    docker rm --force "${container_prefix}-${role}" >/dev/null 2>&1 || true
  done
  if [[ -n "${upstream_pid}" ]] && kill -0 "${upstream_pid}" 2>/dev/null; then
    kill "${upstream_pid}"
    wait "${upstream_pid}" 2>/dev/null || true
  fi
  case "${work_dir}" in
    /tmp/book-scanner-tls-proxy.*) rm -rf -- "${work_dir}" ;;
  esac
  exit "${status}"
}
trap cleanup EXIT

fail() {
  echo "FAIL: $*" >&2
  exit 1
}

pass() {
  echo "ok: $*"
}

# --- Certificates: the internal-CA procedure from docs/operations.md ---------
tls_dir="${work_dir}/tls"
mkdir -p "${tls_dir}"
openssl req -x509 -new -nodes -newkey ec -pkeyopt ec_paramgen_curve:P-256 \
  -keyout "${tls_dir}/serif-ca.key" -out "${tls_dir}/serif-ca.pem" \
  -days 2 -subj "/CN=serif test internal CA" \
  -addext "basicConstraints=critical,CA:TRUE,pathlen:0" \
  -addext "keyUsage=critical,keyCertSign,cRLSign" 2>/dev/null
openssl req -new -nodes -newkey ec -pkeyopt ec_paramgen_curve:P-256 \
  -keyout "${tls_dir}/serif.key" -out "${tls_dir}/serif.csr" \
  -subj "/CN=${host_name}" 2>/dev/null
cat >"${tls_dir}/serif.ext" <<EOF
basicConstraints=critical,CA:FALSE
keyUsage=critical,digitalSignature
extendedKeyUsage=serverAuth
subjectAltName=DNS:${host_name}
EOF
openssl x509 -req -in "${tls_dir}/serif.csr" \
  -CA "${tls_dir}/serif-ca.pem" -CAkey "${tls_dir}/serif-ca.key" -CAcreateserial \
  -out "${tls_dir}/serif.crt" -days 2 -sha256 -extfile "${tls_dir}/serif.ext" 2>/dev/null
openssl verify -CAfile "${tls_dir}/serif-ca.pem" "${tls_dir}/serif.crt" >/dev/null
chmod 0644 "${tls_dir}/serif.key" # throwaway key; the container must read it

# --- Config variants ---------------------------------------------------------
# The shipped file must fail closed: no active allow line.
if grep -Eq '^[[:space:]]*allow[[:space:]]' "${serif_config}"; then
  fail "serif-lan-tls.conf ships an active allow line; it must fail closed"
fi

serif_variant() {
  # $1 = https port, $2 = http port; reads the config on stdin.
  sed \
    -e "s/listen 443 ssl;/listen 127.0.0.1:$1 ssl;/" \
    -e "s/listen 80;/listen 127.0.0.1:$2;/"
}
stub_upstream="upstream book_scanner_asgi { server 127.0.0.1:${upstream_port}; keepalive 4; }"

{
  sed \
    -e "s/server book-scanner-web:8000;/server 127.0.0.1:${upstream_port};/" \
    -e "s/listen 8080;/listen 127.0.0.1:${legacy_http};/" \
    "${plain_config}"
  sed -e 's|# allow 192\.0\.2\.0/24;|allow 127.0.0.1;|' "${serif_config}" \
    | serif_variant "${allowed_https}" "${allowed_http}"
} >"${work_dir}/allowed.conf"
{
  echo "${stub_upstream}"
  sed -E -e 's|^([[:space:]]*)# allow ([0-9./]+);|\1allow \2;|' "${serif_config}" \
    | serif_variant "${ranges_https}" "${ranges_http}"
} >"${work_dir}/ranges.conf"
{
  echo "${stub_upstream}"
  serif_variant "${shipped_https}" "${shipped_http}" <"${serif_config}"
} >"${work_dir}/shipped.conf"

[[ "$(grep -c 'allow 127.0.0.1;' "${work_dir}/allowed.conf")" == "2" ]] \
  || fail "allowed variant was not rewritten in both servers"
[[ "$(grep -c 'allow 192.0.2.0/24;' "${work_dir}/ranges.conf")" == "2" ]] \
  || fail "ranges variant did not enable the example ranges"
if grep -q 'allow 127' "${work_dir}/ranges.conf" "${work_dir}/shipped.conf"; then
  fail "a deny variant unexpectedly allows 127.0.0.1"
fi

# --- Upstream stand-in for Uvicorn ------------------------------------------
# GET answers 200, PUT drains the body and answers 201 with its size.
cat >"${work_dir}/upstream.py" <<'EOF'
import http.server
import json
import sys


class Handler(http.server.BaseHTTPRequestHandler):
    def _reply(self, status, payload):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self._reply(200, {"api_version": 1, "stub": True, "path": self.path})

    def do_PUT(self):
        remaining = int(self.headers.get("Content-Length", "0"))
        received = 0
        while remaining > 0:
            chunk = self.rfile.read(min(remaining, 1 << 20))
            if not chunk:
                break
            received += len(chunk)
            remaining -= len(chunk)
        self._reply(201, {"received": received})

    def log_message(self, *args):
        pass


http.server.ThreadingHTTPServer(("127.0.0.1", int(sys.argv[1])), Handler).serve_forever()
EOF
python3 "${work_dir}/upstream.py" "${upstream_port}" >"${work_dir}/upstream.log" 2>&1 &
upstream_pid=$!

# --- nginx -------------------------------------------------------------------
for role in "${roles[@]}"; do
  docker run --detach --name "${container_prefix}-${role}" --network host \
    -v "${work_dir}/${role}.conf:/etc/nginx/conf.d/default.conf:ro" \
    -v "${tls_dir}:/etc/ssl/serif:ro" \
    "${nginx_image}" >/dev/null
done

wait_for_port() {
  for _ in {1..100}; do
    if (exec 3<>"/dev/tcp/127.0.0.1/$1") 2>/dev/null; then
      return 0
    fi
    sleep 0.2
  done
  fail "nothing listens on 127.0.0.1:$1"
}
for port in "${allowed_https}" "${ranges_https}" "${shipped_https}" "${allowed_http}" \
  "${ranges_http}" "${shipped_http}" "${legacy_http}" "${upstream_port}"; do
  wait_for_port "${port}"
done

https_status() {
  # $1 = port, $2 = path, rest = extra curl arguments; prints the status code.
  local port=$1 path=$2
  shift 2
  curl --silent --show-error --output /dev/null --write-out '%{http_code}' --tlsv1.3 \
    --cacert "${tls_dir}/serif-ca.pem" \
    --resolve "${host_name}:${port}:127.0.0.1" "$@" \
    "https://${host_name}:${port}${path}" || true
}

http_status() {
  # $1 = port, $2 = path; prints "<status> <redirect url>".
  curl --silent --output /dev/null --write-out '%{http_code} %{redirect_url}' \
    -H "Host: ${host_name}" "http://127.0.0.1:$1$2" || true
}

# TLS 1.3 from an allowed source, verified against the CA and the host name.
body="$(curl --silent --show-error --fail --tlsv1.3 \
  --cacert "${tls_dir}/serif-ca.pem" \
  --resolve "${host_name}:${allowed_https}:127.0.0.1" \
  "https://${host_name}:${allowed_https}/api/scanner/v1/ping")"
[[ "${body}" == *'"stub": true'* ]] || fail "TLS 1.3 request did not reach the upstream: ${body}"
pass "TLS 1.3 client from an allowed source reaches /api/scanner/v1/ping"

protocol="$(openssl s_client -connect "127.0.0.1:${allowed_https}" \
  -servername "${host_name}" -CAfile "${tls_dir}/serif-ca.pem" -brief </dev/null 2>&1 \
  | sed -n 's/^Protocol version: *//p')"
[[ "${protocol}" == "TLSv1.3" ]] || fail "negotiated '${protocol}', expected TLSv1.3"
pass "negotiated protocol is TLSv1.3"

# A TLS 1.2-only client must not complete a handshake.
if openssl s_client -connect "127.0.0.1:${allowed_https}" -servername "${host_name}" \
  -tls1_2 </dev/null >"${work_dir}/tls12.log" 2>&1; then
  cat "${work_dir}/tls12.log" >&2
  fail "openssl s_client -tls1_2 completed a handshake"
fi
pass "openssl s_client -tls1_2 cannot connect"

curl_status=0
curl --silent --output /dev/null --cacert "${tls_dir}/serif-ca.pem" \
  --resolve "${host_name}:${allowed_https}:127.0.0.1" --tlsv1.2 --tls-max 1.2 \
  "https://${host_name}:${allowed_https}/api/scanner/v1/ping" || curl_status=$?
((curl_status == 35)) || fail "curl --tls-max 1.2 exited ${curl_status}, expected 35 (handshake failure)"
pass "curl --tls-max 1.2 cannot connect"

# The certificate is for the host name only: a client addressing the IP fails
# verification (sans checks the host name and has no skip option).
curl_status=0
curl --silent --output /dev/null --cacert "${tls_dir}/serif-ca.pem" \
  "https://127.0.0.1:${allowed_https}/api/scanner/v1/ping" || curl_status=$?
((curl_status == 60)) || fail "host-name mismatch exited ${curl_status}, expected 60"
pass "certificate is rejected for a host name it was not issued for"

# Source outside the configured ranges, and the unedited file.
for role in ranges shipped; do
  https_var="${role}_https"
  http_var="${role}_http"
  status="$(https_status "${!https_var}" /api/scanner/v1/ping)"
  [[ "${status}" == "403" ]] || fail "${role}: HTTPS got ${status}, expected 403"
  status="$(https_status "${!https_var}" /library/)"
  [[ "${status}" == "403" ]] || fail "${role}: HTTPS portal got ${status}, expected 403"
  result="$(http_status "${!http_var}" /library/)"
  [[ "${result}" == "403 "* ]] || fail "${role}: HTTP got '${result}', expected 403"
done
pass "source outside the allowed ranges gets 403 on HTTPS and HTTP"
pass "unedited serif-lan-tls.conf fails closed (403 for everyone)"

# Body size on /api/scanner/: above nginx's 1 MiB default passes to the
# upstream (the stub drains it and answers 201), above 64 MiB is refused by nginx.
head -c $((2 * 1024 * 1024)) /dev/zero >"${work_dir}/2m.bin"
head -c $((64 * 1024 * 1024 + 1)) /dev/zero >"${work_dir}/64m-plus-1.bin"
status="$(https_status "${allowed_https}" /api/scanner/v1/jobs/x/pages/0 \
  --upload-file "${work_dir}/2m.bin")"
[[ "${status}" == "201" ]] || fail "2 MiB scanner upload got ${status}, expected the stub's 201"
status="$(https_status "${allowed_https}" /api/scanner/v1/jobs/x/pages/0 \
  --upload-file "${work_dir}/64m-plus-1.bin")"
[[ "${status}" == "413" ]] || fail "64 MiB + 1 byte scanner upload got ${status}, expected 413"
pass "/api/scanner/ accepts 2 MiB and rejects 64 MiB + 1 byte with 413"

# Plain HTTP: browsers are redirected to HTTPS, the device API is refused.
result="$(http_status "${allowed_http}" /library/)"
[[ "${result}" == "301 https://${host_name}/library/" ]] || fail "HTTP redirect was '${result}'"
result="$(http_status "${allowed_http}" /api/scanner/v1/ping)"
[[ "${result}" == "403 "* ]] || fail "port-80 scanner API got '${result}', expected 403"
pass "port-80 server redirects browsers and refuses /api/scanner/"

# book-scanner.conf's plain-HTTP listener still proxies the site but never the API.
result="$(http_status "${legacy_http}" /library/)"
[[ "${result}" == "200 "* ]] || fail "book-scanner.conf /library/ got '${result}', expected 200"
result="$(http_status "${legacy_http}" /api/scanner/v1/ping)"
[[ "${result}" == "403 "* ]] || fail "book-scanner.conf scanner API got '${result}', expected 403"
pass "book-scanner.conf plain-HTTP listener refuses /api/scanner/"

echo "scanner TLS proxy checks passed"
