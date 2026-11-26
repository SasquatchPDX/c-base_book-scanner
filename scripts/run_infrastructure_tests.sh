#!/usr/bin/env bash
set -Eeuo pipefail

book_scanner_project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
book_scanner_compose_file="${book_scanner_project_root}/compose.integration.yml"
book_scanner_storage_root="$(mktemp -d -t book-scanner-integration.XXXXXX)"
book_scanner_pandoc_root="$(mktemp -d -t book-scanner-pandoc.XXXXXX)"
book_scanner_calibre_root="$(mktemp -d -t book-scanner-calibre.XXXXXX)"
book_scanner_postgres_port="${BOOK_SCANNER_INTEGRATION_POSTGRES_PORT:-15432}"
book_scanner_redis_port="${BOOK_SCANNER_INTEGRATION_REDIS_PORT:-16379}"
book_scanner_ldap_port="${BOOK_SCANNER_INTEGRATION_LDAP_PORT:-13389}"
book_scanner_proxy_port="${BOOK_SCANNER_INTEGRATION_PROXY_PORT:-18080}"
book_scanner_ocr_port="${BOOK_SCANNER_INTEGRATION_OCR_PORT:-18082}"
book_scanner_uvicorn_pid=""
book_scanner_uvicorn_log="$(mktemp -t book-scanner-uvicorn.XXXXXX.log)"
book_scanner_ocr_pid=""
book_scanner_ocr_log="$(mktemp -t book-scanner-ocr.XXXXXX.log)"
book_scanner_worker_pid=""
book_scanner_worker_log="$(mktemp -t book-scanner-worker.XXXXXX.log)"

book_scanner_compose=(
  docker compose
  --project-name book-scanner-integration
  --file "${book_scanner_compose_file}"
)

book_scanner_cleanup() {
  book_scanner_exit_status=$?
  trap - EXIT
  if ((book_scanner_exit_status != 0)); then
    "${book_scanner_compose[@]}" ps || true
    "${book_scanner_compose[@]}" logs --no-color || true
    tail -200 "${book_scanner_uvicorn_log}" || true
    tail -200 "${book_scanner_worker_log}" || true
    tail -200 "${book_scanner_ocr_log}" || true
  fi
  if [[ -n "${book_scanner_uvicorn_pid}" ]] && kill -0 "${book_scanner_uvicorn_pid}" 2>/dev/null; then
    kill "${book_scanner_uvicorn_pid}"
    wait "${book_scanner_uvicorn_pid}" || true
  fi
  if [[ -n "${book_scanner_worker_pid}" ]] && kill -0 "${book_scanner_worker_pid}" 2>/dev/null; then
    kill "${book_scanner_worker_pid}"
    wait "${book_scanner_worker_pid}" || true
  fi
  if [[ -n "${book_scanner_ocr_pid}" ]] && kill -0 "${book_scanner_ocr_pid}" 2>/dev/null; then
    kill "${book_scanner_ocr_pid}"
    wait "${book_scanner_ocr_pid}" || true
  fi
  "${book_scanner_compose[@]}" down --volumes --remove-orphans
  case "${book_scanner_storage_root}" in
    /tmp/book-scanner-integration.*) rm -rf -- "${book_scanner_storage_root}" ;;
  esac
  case "${book_scanner_pandoc_root}" in
    /tmp/book-scanner-pandoc.*) rm -rf -- "${book_scanner_pandoc_root}" ;;
  esac
  case "${book_scanner_calibre_root}" in
    /tmp/book-scanner-calibre.*) rm -rf -- "${book_scanner_calibre_root}" ;;
  esac
  case "${book_scanner_uvicorn_log}" in
    /tmp/book-scanner-uvicorn.*.log) rm -f -- "${book_scanner_uvicorn_log}" ;;
  esac
  case "${book_scanner_worker_log}" in
    /tmp/book-scanner-worker.*.log) rm -f -- "${book_scanner_worker_log}" ;;
  esac
  case "${book_scanner_ocr_log}" in
    /tmp/book-scanner-ocr.*.log) rm -f -- "${book_scanner_ocr_log}" ;;
  esac
  exit "${book_scanner_exit_status}"
}
trap book_scanner_cleanup EXIT

export BOOK_SCANNER_INTEGRATION_POSTGRES_PORT="${book_scanner_postgres_port}"
export BOOK_SCANNER_INTEGRATION_REDIS_PORT="${book_scanner_redis_port}"
export BOOK_SCANNER_INTEGRATION_LDAP_PORT="${book_scanner_ldap_port}"
export BOOK_SCANNER_INTEGRATION_PROXY_PORT="${book_scanner_proxy_port}"
export BOOK_SCANNER_DATABASE_URL="postgresql://book_scanner:book_scanner_integration@127.0.0.1:${book_scanner_postgres_port}/book_scanner_integration"
export BOOK_SCANNER_REDIS_URL="redis://127.0.0.1:${book_scanner_redis_port}/15"
export BOOK_SCANNER_STORAGE_ROOT="${book_scanner_storage_root}"
export BOOK_SCANNER_LDAP_MODE="ldap"
export BOOK_SCANNER_LDAP_SERVER_URI="ldap://127.0.0.1:${book_scanner_ldap_port}"
export BOOK_SCANNER_LDAP_USER_DN_TEMPLATE=""
export BOOK_SCANNER_LDAP_USER_SEARCH_BASE="ou=people,dc=planetexpress,dc=com"
export BOOK_SCANNER_LDAP_USER_SEARCH_FILTER="(uid={username})"
export BOOK_SCANNER_LDAP_SERVICE_BIND_DN="cn=admin,dc=planetexpress,dc=com"
export BOOK_SCANNER_LDAP_SERVICE_BIND_PASSWORD="GoodNewsEveryone"
export BOOK_SCANNER_LDAP_EMAIL_ATTRIBUTE="mail"
export BOOK_SCANNER_CSRF_TRUSTED_ORIGINS="[\"http://127.0.0.1:${book_scanner_proxy_port}\"]"
export BOOK_SCANNER_RUN_INFRASTRUCTURE_TESTS="true"
export BOOK_SCANNER_METADATA_PROVIDERS='["tests.browser_fixtures.BrowserMetadataProvider"]'
# BrowserMetadataProvider echoes the searched title and author, i.e. an exact title +
# strong author match that the #9 rule would auto-accept. The digital-library browser
# scenario exercises the manual review workflow, so keep that rule off here; it is
# covered by tests/test_identification.py and tests/test_intake.py.
export BOOK_SCANNER_IDENTIFICATION_STRONG_MATCH_ENABLED="false"
export BOOK_SCANNER_OCR_SERVICE_URL="http://127.0.0.1:${book_scanner_ocr_port}"
export BOOK_SCANNER_OCR_REQUEST_TIMEOUT="10"
export BOOK_SCANNER_OCR_MAX_ATTEMPTS="5"
export BOOK_SCANNER_OCR_POLL_INTERVAL_SECONDS="0.1"
export QT_QPA_PLATFORM="offscreen"

cd "${book_scanner_project_root}"
# TLS 1.3-only, LAN-only scanner front end (deploy/nginx/serif-lan-tls.conf); it also
# runs scripts/check_scanner_onboarding.sh against that nginx.
"${book_scanner_project_root}/scripts/test_scanner_tls_proxy.sh"
if ! command -v pandoc >/dev/null 2>&1; then
  "${book_scanner_project_root}/scripts/install_test_pandoc.sh" "${book_scanner_pandoc_root}"
  export PATH="${book_scanner_pandoc_root}/bin:${PATH}"
fi
if ! command -v ebook-convert >/dev/null 2>&1; then
  "${book_scanner_project_root}/scripts/install_test_calibre.sh" "${book_scanner_calibre_root}"
  export PATH="${book_scanner_calibre_root}/calibre:${PATH}"
fi
book_scanner_browser_epub="${book_scanner_storage_root}/browser-digital-library.epub"
pandoc \
  "${book_scanner_project_root}/tests/fixtures/browser-digital-library.html" \
  --from html \
  --to epub3 \
  --metadata title="Browser Digital Library" \
  --metadata author="Ada Digital" \
  --metadata lang=en-US \
  --metadata publisher="Ada Digital Self-Publishing" \
  --output "${book_scanner_browser_epub}"
export BOOK_SCANNER_BROWSER_EPUB_FIXTURE="${book_scanner_browser_epub}"
export BOOK_SCANNER_BROWSER_TIED_EPUB="${book_scanner_storage_root}/Browser Edition Twins.epub"
pandoc "${book_scanner_project_root}/tests/fixtures/browser-digital-library.html" \
  --from html --to epub3 --metadata title="Browser Edition Twins" \
  --metadata author="Ada Digital" --metadata lang=en --output "${BOOK_SCANNER_BROWSER_TIED_EPUB}"
uv run python manage.py collectstatic --noinput --clear --verbosity 0
"${book_scanner_compose[@]}" up --detach --wait --wait-timeout 120
uv run python manage.py migrate --noinput
uv run python manage.py shell -c 'from tests.browser_fixtures import seed_worker_failure_review; seed_worker_failure_review()'
uv run python manage.py shell -c 'from tests.browser_fixtures import seed_migrated_scan_page; seed_migrated_scan_page()'
uv run python manage.py check
# tests/integration includes test_rehearse_migrations_postgres.py, which runs
# scripts/rehearse_migrations.sh end to end against this PostgreSQL (pg_dump and friends
# run from the pinned postgres image unless BOOK_SCANNER_REHEARSAL_PG_BINDIR is set).
uv run pytest -q tests/integration --create-db

# The simulated scanner and web app share a fresh device key for this run.
# Keep the normal HTTP listener's scanner denial. The extra TLS listener is
# published only on loopback and uses a new certificate trusted by this test.
tls_dir="${book_scanner_storage_root}/device-tls"
mkdir -p "${tls_dir}"
openssl req -x509 -nodes -newkey ec -pkeyopt ec_paramgen_curve:P-256 -days 1 \
  -subj /CN=127.0.0.1 -addext subjectAltName=IP:127.0.0.1 \
  -keyout "${tls_dir}/device.key" -out "${tls_dir}/device.crt" 2>/dev/null
cat >"${tls_dir}/device.conf" <<'NGINX'
server {
    listen 8443 ssl;
    ssl_protocols TLSv1.3;
    ssl_certificate /etc/nginx/device.crt;
    ssl_certificate_key /etc/nginx/device.key;
    client_max_body_size 64m;
    location /api/scanner/ {
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Proto https;
        proxy_pass http://book_scanner_asgi;
    }
    location / { return 404; }
}
NGINX
"${book_scanner_compose[@]}" cp "${tls_dir}/device.key" proxy:/etc/nginx/device.key
"${book_scanner_compose[@]}" cp "${tls_dir}/device.crt" proxy:/etc/nginx/device.crt
"${book_scanner_compose[@]}" cp "${tls_dir}/device.conf" proxy:/etc/nginx/conf.d/device.conf
"${book_scanner_compose[@]}" exec -T proxy nginx -s reload
export NODE_EXTRA_CA_CERTS="${tls_dir}/device.crt"
export BOOK_SCANNER_BROWSER_DEVICE_URL="https://$("${book_scanner_compose[@]}" port proxy 8443)"
export BOOK_SCANNER_BROWSER_DEVICE_KEY="$(uv run python -c 'import secrets; print(secrets.token_urlsafe(32))')"
export BOOK_SCANNER_SCANNER_API_KEY_SHA256="$(uv run python -c 'import hashlib, os; print(hashlib.sha256(os.environ["BOOK_SCANNER_BROWSER_DEVICE_KEY"].encode()).hexdigest())')"
export BOOK_SCANNER_PUBLIC_BASE_URL="http://127.0.0.1:${book_scanner_proxy_port}/"
export BOOK_SCANNER_SCANTAILOR_EVENT_STREAM_INTERVAL_SECONDS="0.2"
export BOOK_SCANNER_SCANTAILOR_EVENT_STREAM_MAX_EVENTS="150"
uv run uvicorn tests.browser_fixtures:ocr_app \
  --host 127.0.0.1 \
  --port "${book_scanner_ocr_port}" \
  --log-level warning \
  --no-access-log \
  --no-server-header \
  >"${book_scanner_ocr_log}" 2>&1 &
book_scanner_ocr_pid=$!

book_scanner_ocr_ready="false"
for _ in {1..100}; do
  if ! kill -0 "${book_scanner_ocr_pid}" 2>/dev/null; then
    break
  fi
  if curl --fail --silent --show-error "http://127.0.0.1:${book_scanner_ocr_port}/healthz" >/dev/null; then
    book_scanner_ocr_ready="true"
    break
  fi
  sleep 0.2
done
if [[ "${book_scanner_ocr_ready}" != "true" ]]; then
  echo "OCR contract fixture failed to become ready for browser acceptance" >&2
  exit 1
fi

uv run rq worker \
  --with-scheduler \
  --config ebook_server.rq_settings \
  --worker-class ebook_server.recovery_worker.RecoveryWorker \
  --url "${BOOK_SCANNER_REDIS_URL}" \
  metadata ebook-server \
  >"${book_scanner_worker_log}" 2>&1 &
book_scanner_worker_pid=$!

uv run uvicorn ebook_server.asgi:application \
  --host 0.0.0.0 \
  --port 18081 \
  --log-level warning \
  --no-access-log \
  --no-server-header \
  >"${book_scanner_uvicorn_log}" 2>&1 &
book_scanner_uvicorn_pid=$!

book_scanner_uvicorn_ready="false"
for _ in {1..100}; do
  if ! kill -0 "${book_scanner_uvicorn_pid}" 2>/dev/null; then
    break
  fi
  if curl --fail --silent --show-error "http://127.0.0.1:18081/healthz/" >/dev/null; then
    book_scanner_uvicorn_ready="true"
    break
  fi
  sleep 0.2
done
if [[ "${book_scanner_uvicorn_ready}" != "true" ]]; then
  echo "Uvicorn failed to become ready for browser acceptance" >&2
  exit 1
fi

export BOOK_SCANNER_BROWSER_BASE_URL="http://127.0.0.1:${book_scanner_proxy_port}"
npm run test:e2e
