#!/usr/bin/env bash
# Rehearse pending Django migrations against a throwaway COPY of a production database.
#
#   SOURCE_DATABASE_URL=postgresql://reader@db.lan/book_scanner \
#   ADMIN_DATABASE_URL=postgresql://admin@db.lan/postgres \
#     scripts/rehearse_migrations.sh [--keep] [--sample N] [--scratch-name NAME]
#
# The source database is only ever read, by pg_dump (in a read-only transaction).
# ADMIN_DATABASE_URL names a maintenance database on the server that will hold the
# scratch copy; its role needs CREATEDB and must be able to restore the dump (create the
# extensions the app uses). Steps: pg_dump -> createdb <scratch> -> pg_restore ->
# snapshot -> manage.py migrate (timed) -> makemigrations --check, migrate --check,
# check_scan_job_integrity --compare -> PASS/FAIL report -> dropdb <scratch>.
#
# Run it from the checkout, with the deployment's environment (.env or EnvironmentFile)
# loaded: only BOOK_SCANNER_DATABASE_URL is overridden, to point at the scratch copy.
# Passwords never appear on a command line or in the output: they are taken out of the
# URLs and handed to the PostgreSQL tools through PGPASSWORD.
#
# Optional environment: PG_BINDIR (directory holding pg_dump/pg_restore/createdb/dropdb
# of a version >= the server's), REHEARSAL_WORK_DIR (parent for the temporary dump).
set -euo pipefail

usage() {
  sed -n '2,21p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
}

keep="false"
sample="20"
scratch_name=""
while (($# > 0)); do
  case "$1" in
    --keep) keep="true" ;;
    --sample)
      sample="${2:?--sample needs a number}"
      shift
      ;;
    --scratch-name)
      scratch_name="${2:?--scratch-name needs a name}"
      shift
      ;;
    -h | --help)
      usage
      exit 0
      ;;
    *)
      echo "unknown argument: $1 (see --help)" >&2
      exit 2
      ;;
  esac
  shift
done

if [[ -z "${SOURCE_DATABASE_URL:-}" || -z "${ADMIN_DATABASE_URL:-}" ]]; then
  echo "SOURCE_DATABASE_URL and ADMIN_DATABASE_URL must both be set (see --help)" >&2
  exit 2
fi
if ! [[ "${sample}" =~ ^[0-9]+$ ]]; then
  echo "--sample must be a non-negative integer" >&2
  exit 2
fi
if [[ -z "${scratch_name}" ]]; then
  scratch_name="book_scanner_rehearsal_$(date -u +%Y%m%d%H%M%S)_$$_${RANDOM}"
fi
if ! [[ "${scratch_name}" =~ ^[a-z_][a-z0-9_]{0,62}$ ]]; then
  echo "scratch database name must match [a-z_][a-z0-9_]{0,62}" >&2
  exit 2
fi

project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${project_root}"

pg_tool() {
  local tool="$1"
  if [[ -n "${PG_BINDIR:-}" ]]; then
    printf '%s/%s' "${PG_BINDIR}" "${tool}"
  else
    printf '%s' "${tool}"
  fi
}
pg_dump_bin="$(pg_tool pg_dump)"
pg_restore_bin="$(pg_tool pg_restore)"
createdb_bin="$(pg_tool createdb)"
dropdb_bin="$(pg_tool dropdb)"
for tool in "${pg_dump_bin}" "${pg_restore_bin}" "${createdb_bin}" "${dropdb_bin}" uv; do
  if ! command -v "${tool}" >/dev/null 2>&1; then
    echo "required tool not found: ${tool}" >&2
    exit 2
  fi
done

# Split the URLs without echoing them: password-free URLs for command lines, passwords
# for PGPASSWORD, and the full scratch URL for Django (environment only).
# shellcheck disable=SC2016
url_helper='
import os
import sys
from urllib.parse import quote, unquote, urlsplit, urlunsplit

LOCAL = {"", "localhost", "127.0.0.1", "::1"}


def parse(name):
    url = urlsplit(os.environ[name])
    if url.scheme not in ("postgres", "postgresql"):
        sys.exit(f"{name} must be a postgresql:// URL")
    host = (url.hostname or "").lower()
    try:
        port = url.port or 5432
    except ValueError:
        sys.exit(f"{name} has an invalid port")
    database = unquote(url.path.lstrip("/"))
    if not database:
        sys.exit(f"{name} must name a database")
    user = quote(unquote(url.username), safe="") if url.username else ""
    hostport = url.netloc.rpartition("@")[2]
    password = unquote(url.password) if url.password is not None else ""
    server = ("localhost" if host in LOCAL else host, port)
    return url, user, hostport, password, database, server


def build(url, user, hostport, password, database):
    netloc = hostport
    if user:
        userinfo = user + (":" + quote(password, safe="") if password else "")
        netloc = userinfo + "@" + hostport
    return urlunsplit((url.scheme, netloc, "/" + quote(database, safe=""), url.query, ""))


src, src_user, src_hostport, src_password, src_db, src_server = parse("SOURCE_DATABASE_URL")
adm, adm_user, adm_hostport, adm_password, adm_db, adm_server = parse("ADMIN_DATABASE_URL")
scratch = os.environ["REHEARSAL_SCRATCH_NAME"]
for value in (src_password, adm_password):
    if "\n" in value:
        sys.exit("passwords containing newlines are not supported")
lines = [
    build(src, src_user, src_hostport, "", src_db),
    src_password,
    src_db,
    build(adm, adm_user, adm_hostport, "", adm_db),
    adm_password,
    adm_db,
    build(adm, adm_user, adm_hostport, adm_password, scratch),
    build(adm, adm_user, adm_hostport, "", scratch),
    "same-server" if src_server == adm_server else "other-server",
]
sys.stdout.write("\n".join(lines) + "\n")
'
url_fields=()
if ! url_output="$(REHEARSAL_SCRATCH_NAME="${scratch_name}" uv run --quiet python -I -c "${url_helper}")"; then
  echo "could not parse the database URLs" >&2
  exit 2
fi
url_output="${url_output//$'\r'/}"
mapfile -t url_fields <<<"${url_output}"
if ((${#url_fields[@]} != 9)); then
  echo "could not parse the database URLs" >&2
  exit 2
fi
unset url_output
source_url="${url_fields[0]}"
source_password="${url_fields[1]}"
source_db="${url_fields[2]}"
admin_url="${url_fields[3]}"
admin_password="${url_fields[4]}"
admin_db="${url_fields[5]}"
scratch_django_url="${url_fields[6]}"
scratch_url="${url_fields[7]}"
server_relation="${url_fields[8]}"
unset url_fields

if [[ "${scratch_name}" == "${source_db}" ]]; then
  echo "REFUSED: the scratch database name equals the source database name" >&2
  exit 3
fi
if [[ "${server_relation}" == "same-server" && "${admin_db}" == "${source_db}" ]]; then
  echo "REFUSED: SOURCE_DATABASE_URL and ADMIN_DATABASE_URL point to the same database" >&2
  exit 3
fi
if [[ "${scratch_name}" == "${admin_db}" ]]; then
  echo "REFUSED: the scratch database name equals the admin database name" >&2
  exit 3
fi

umask 077
work_dir="$(mktemp -d "${REHEARSAL_WORK_DIR:-${TMPDIR:-/tmp}}/book-scanner-rehearsal.XXXXXX")"
dump_file="${work_dir}/source.dump"
snapshot_file="${work_dir}/before.json"
scratch_created="false"
report=()
overall="PASS"

now_ms() {
  local now="${EPOCHREALTIME:-}"
  if [[ -n "${now}" ]]; then
    now="${now/[.,]/}"
    printf '%s' "$((10#${now} / 1000))"
  else
    printf '%s' "$(($(date +%s) * 1000))"
  fi
}

seconds_since() {
  local elapsed=$(($(now_ms) - $1))
  printf '%d.%03ds' "$((elapsed / 1000))" "$((elapsed % 1000))"
}

record() {
  # record STATUS NAME DETAIL
  report+=("$(printf '%-4s  %-38s %s' "$1" "$2" "$3")")
  if [[ "$1" == "FAIL" ]]; then
    overall="FAIL"
  fi
  printf '==> %s: %s %s\n' "$1" "$2" "$3"
}

# run_step NAME COMMAND...: run, time and record; returns the command's status.
run_step() {
  local name="$1"
  shift
  local started
  started="$(now_ms)"
  printf '==> %s\n' "${name}"
  if "$@"; then
    record "PASS" "${name}" "($(seconds_since "${started}"))"
    return 0
  fi
  record "FAIL" "${name}" "($(seconds_since "${started}"))"
  return 1
}

as_source() {
  PGPASSWORD="${source_password}" PGOPTIONS="-c default_transaction_read_only=on" "$@"
}

as_admin() {
  PGPASSWORD="${admin_password}" "$@"
}

manage() {
  BOOK_SCANNER_DATABASE_URL="${scratch_django_url}" uv run python manage.py "$@"
}

print_report() {
  printf '\n=== Migration rehearsal report ===\n'
  printf 'source database:  %s\n' "${source_db}"
  printf 'scratch database: %s (%s)\n' "${scratch_name}" "${server_relation}"
  local line
  for line in "${report[@]}"; do
    printf '%s\n' "${line}"
  done
  printf 'RESULT: %s\n' "${overall}"
}

cleanup() {
  local status=$?
  trap - EXIT
  if [[ "${scratch_created}" == "true" ]]; then
    if [[ "${keep}" == "true" ]]; then
      printf 'kept scratch database %s (drop it with dropdb when done)\n' "${scratch_name}"
    elif as_admin "${dropdb_bin}" --if-exists --force --maintenance-db="${admin_url}" \
      "${scratch_name}"; then
      printf 'dropped scratch database %s\n' "${scratch_name}"
    else
      printf 'WARNING: could not drop scratch database %s\n' "${scratch_name}" >&2
      status=1
    fi
  fi
  rm -rf -- "${work_dir}"
  exit "${status}"
}
trap cleanup EXIT

printf 'Rehearsing migrations: source %s -> scratch %s\n' "${source_db}" "${scratch_name}"
rehearsal_started="$(now_ms)"

prepared="false"
if run_step "pg_dump source (read-only)" \
  as_source "${pg_dump_bin}" --format=custom --no-password --file="${dump_file}" \
  --dbname="${source_url}"; then
  if run_step "createdb scratch" \
    as_admin "${createdb_bin}" --no-password --maintenance-db="${admin_url}" \
    "${scratch_name}"; then
    scratch_created="true"
    if run_step "pg_restore into scratch" \
      as_admin "${pg_restore_bin}" --no-password --no-owner --no-privileges \
      --exit-on-error --dbname="${scratch_url}" "${dump_file}"; then
      prepared="true"
    fi
  fi
fi

if [[ "${prepared}" == "true" ]]; then
  printf '==> pending migrations\n'
  manage showmigrations --plan | grep -F '[ ]' || printf '    (none)\n'
  if run_step "pre-migration snapshot" \
    manage check_scan_job_integrity --snapshot "${snapshot_file}"; then
    if run_step "manage.py migrate" manage migrate --noinput; then
      run_step "migrate --check (nothing unapplied)" manage migrate --check || true
      run_step "makemigrations --check" manage makemigrations --check --dry-run || true
      run_step "scan job integrity + sample URLs" \
        manage check_scan_job_integrity --compare "${snapshot_file}" --sample "${sample}" ||
        true
    fi
  fi
fi

record "$([[ "${overall}" == "PASS" ]] && echo "PASS" || echo "FAIL")" \
  "total" "($(seconds_since "${rehearsal_started}"))"
print_report
[[ "${overall}" == "PASS" ]]
