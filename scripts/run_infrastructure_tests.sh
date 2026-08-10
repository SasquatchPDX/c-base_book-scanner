#!/usr/bin/env bash
set -Eeuo pipefail

book_scanner_project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
book_scanner_compose_file="${book_scanner_project_root}/compose.integration.yml"
book_scanner_storage_root="$(mktemp -d -t book-scanner-integration.XXXXXX)"
book_scanner_pandoc_root="$(mktemp -d -t book-scanner-pandoc.XXXXXX)"
book_scanner_postgres_port="${BOOK_SCANNER_INTEGRATION_POSTGRES_PORT:-55432}"
book_scanner_redis_port="${BOOK_SCANNER_INTEGRATION_REDIS_PORT:-56379}"
book_scanner_ldap_port="${BOOK_SCANNER_INTEGRATION_LDAP_PORT:-55389}"

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
  fi
  "${book_scanner_compose[@]}" down --volumes --remove-orphans
  case "${book_scanner_storage_root}" in
    /tmp/book-scanner-integration.*) rm -rf -- "${book_scanner_storage_root}" ;;
  esac
  case "${book_scanner_pandoc_root}" in
    /tmp/book-scanner-pandoc.*) rm -rf -- "${book_scanner_pandoc_root}" ;;
  esac
  exit "${book_scanner_exit_status}"
}
trap book_scanner_cleanup EXIT

export BOOK_SCANNER_INTEGRATION_POSTGRES_PORT="${book_scanner_postgres_port}"
export BOOK_SCANNER_INTEGRATION_REDIS_PORT="${book_scanner_redis_port}"
export BOOK_SCANNER_INTEGRATION_LDAP_PORT="${book_scanner_ldap_port}"
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
export BOOK_SCANNER_RUN_INFRASTRUCTURE_TESTS="true"

cd "${book_scanner_project_root}"
if ! command -v pandoc >/dev/null 2>&1; then
  "${book_scanner_project_root}/scripts/install_test_pandoc.sh" "${book_scanner_pandoc_root}"
  export PATH="${book_scanner_pandoc_root}/bin:${PATH}"
fi
"${book_scanner_compose[@]}" up --detach --wait --wait-timeout 120
uv run python manage.py migrate --noinput
uv run python manage.py check
uv run pytest -q tests/integration --create-db
