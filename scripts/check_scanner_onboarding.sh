#!/usr/bin/env bash
# Verify a libreflip scanner onboarding end to end (docs/operations.md, "Onboarding a
# scanner"). Run it on the VM, or on the Pi with --pi as the user that runs sans.
#
#   scripts/check_scanner_onboarding.sh --url https://serif.lan.example/ \
#     --ca-file ~/.config/sans/serif-ca.pem --key-file ~/.config/sans/serif-api-key [--pi]
#
# Checks (each prints PASS, FAIL or SKIP):
#   key-file-mode   the key file is mode 0600 (with --pi; sans refuses anything else)
#   tls13           TLS 1.3 handshake verifying the CA and the host name (openssl)
#   tls12-refused   a TLS 1.2-only client cannot complete a handshake
#   ping            GET /api/scanner/v1/ping with the Bearer key -> 200 with
#                   api_version, server_time and limits
#   ping-no-key     the same request without the key -> 401
#   plain-http      /api/scanner/ over the plain-HTTP listener -> 403
#   clock-skew      |server_time - local clock| < --max-skew seconds (default 2)
#   ntp-synced      timedatectl reports the local clock as NTP-synchronised
#
# Options:
#   --url URL          HTTPS base URL (or SERIF_URL)
#   --ca-file PATH     CA certificate (or SERIF_CA_FILE)
#   --key-file PATH    file holding the scanner API key (or SERIF_KEY_FILE)
#   --http-url URL     plain-HTTP base URL to probe (default: http://<host>/; on the VM
#                      also try book-scanner.conf's listener, e.g. http://127.0.0.1:8080/)
#   --resolve ADDRESS  connect to ADDRESS instead of resolving the host name (the
#                      certificate is still checked against the host name)
#   --pi               also check the key file mode (run on the scanner)
#   --max-skew SECONDS clock-skew limit (default 2)
#   --no-ntp-status    skip the timedatectl check
#   --dry-run          validate the arguments and list the checks; no network access
#
# The key is read from the file and passed to curl through a process substitution: it
# never appears on a command line, in the process list or in the output.
# Needs bash, openssl, curl and python3.
set -euo pipefail

usage() {
  sed -n '2,33p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
}

base_url="${SERIF_URL:-}"
ca_file="${SERIF_CA_FILE:-}"
key_file="${SERIF_KEY_FILE:-}"
http_url=""
resolve_address=""
pi_side="false"
max_skew="2"
ntp_status="true"
dry_run="false"
while (($# > 0)); do
  case "$1" in
    --url) base_url="${2:?--url needs a value}" && shift ;;
    --ca-file) ca_file="${2:?--ca-file needs a value}" && shift ;;
    --key-file) key_file="${2:?--key-file needs a value}" && shift ;;
    --http-url) http_url="${2:?--http-url needs a value}" && shift ;;
    --resolve) resolve_address="${2:?--resolve needs an address}" && shift ;;
    --pi) pi_side="true" ;;
    --max-skew) max_skew="${2:?--max-skew needs a number}" && shift ;;
    --no-ntp-status) ntp_status="false" ;;
    --dry-run) dry_run="true" ;;
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

die() {
  echo "$*" >&2
  exit 2
}

[[ -n "${base_url}" ]] || die "--url (or SERIF_URL) is required"
[[ -n "${ca_file}" ]] || die "--ca-file (or SERIF_CA_FILE) is required"
[[ -n "${key_file}" ]] || die "--key-file (or SERIF_KEY_FILE) is required"
[[ -r "${ca_file}" ]] || die "CA file is not readable: ${ca_file}"
[[ -r "${key_file}" ]] || die "key file is not readable: ${key_file}"
[[ "${max_skew}" =~ ^[0-9]+([.][0-9]+)?$ ]] || die "--max-skew must be a number of seconds"

url_pattern='^https://([A-Za-z0-9.-]+)(:([0-9]+))?(/.*)?$'
[[ "${base_url}" =~ ${url_pattern} ]] || die "--url must look like https://host[:port]/"
host_name="${BASH_REMATCH[1]}"
https_port="${BASH_REMATCH[3]:-443}"
base_path="${BASH_REMATCH[4]:-/}"
base_path="${base_path%/}"
ping_url="https://${host_name}:${https_port}${base_path}/api/scanner/v1/ping"

if [[ -z "${http_url}" ]]; then
  http_url="http://${host_name}/"
fi
http_pattern='^http://([A-Za-z0-9.-]+)(:([0-9]+))?(/.*)?$'
[[ "${http_url}" =~ ${http_pattern} ]] || die "--http-url must look like http://host[:port]/"
http_host="${BASH_REMATCH[1]}"
http_port="${BASH_REMATCH[3]:-80}"
http_path="${BASH_REMATCH[4]:-/}"
plain_ping_url="http://${http_host}:${http_port}${http_path%/}/api/scanner/v1/ping"

connect_address="${resolve_address:-${host_name}}"
curl_resolve=()
if [[ -n "${resolve_address}" ]]; then
  curl_resolve=(
    --resolve "${host_name}:${https_port}:${resolve_address}"
    --resolve "${http_host}:${http_port}:${resolve_address}"
  )
fi

results=()
overall="PASS"
report() {
  # report STATUS CHECK DETAIL
  local line
  line="$(printf '%-4s  %-14s %s' "$1" "$2" "$3")"
  results+=("${line}")
  printf '%s\n' "${line}"
  if [[ "$1" == "FAIL" ]]; then
    overall="FAIL"
  fi
}

file_mode() {
  stat -c '%a' -- "$1" 2>/dev/null || stat -f '%Lp' -- "$1"
}

if [[ "${dry_run}" == "true" ]]; then
  printf 'DRY RUN: no connections are made.\n'
  printf 'host name:    %s (connect to %s, port %s)\n' "${host_name}" "${connect_address}" \
    "${https_port}"
  printf 'ping URL:     %s\n' "${ping_url}"
  printf 'plain HTTP:   %s\n' "${plain_ping_url}"
  printf 'CA file:      %s\n' "${ca_file}"
  printf 'key file:     %s (contents not shown)\n' "${key_file}"
  printf 'checks:      '
  [[ "${pi_side}" == "true" ]] && printf ' key-file-mode'
  printf ' tls13 tls12-refused ping ping-no-key plain-http clock-skew'
  [[ "${ntp_status}" == "true" ]] && printf ' ntp-synced'
  printf '\n'
  exit 0
fi

for tool in openssl curl python3; do
  command -v "${tool}" >/dev/null 2>&1 || die "required tool not found: ${tool}"
done

work_dir="$(mktemp -d "${TMPDIR:-/tmp}/scanner-onboarding.XXXXXX")"
trap 'rm -rf -- "${work_dir}"' EXIT

# --- key file ------------------------------------------------------------------
if [[ "${pi_side}" == "true" ]]; then
  mode="$(file_mode "${key_file}")"
  if [[ "${mode}" == "600" ]]; then
    report PASS key-file-mode "${key_file} is 0600"
  else
    report FAIL key-file-mode "${key_file} is 0${mode}; run: chmod 0600 ${key_file}"
  fi
else
  report SKIP key-file-mode "not on the scanner (use --pi there)"
fi

api_key="$(tr -d '[:space:]' <"${key_file}")"
if [[ -z "${api_key}" ]]; then
  report FAIL ping "key file is empty"
fi

# --- TLS ---------------------------------------------------------------------------
tls13_log="${work_dir}/tls13.log"
if openssl s_client -connect "${connect_address}:${https_port}" -servername "${host_name}" \
  -CAfile "${ca_file}" -verify_hostname "${host_name}" -verify_return_error \
  -tls1_3 -brief </dev/null >"${tls13_log}" 2>&1 \
  && grep -q '^Protocol version: *TLSv1.3' "${tls13_log}"; then
  report PASS tls13 "TLS 1.3 handshake verified against the CA for ${host_name}"
else
  report FAIL tls13 "$(grep -m1 -iE 'error|verify|refused' "${tls13_log}" || echo 'handshake failed')"
fi

if openssl s_client -connect "${connect_address}:${https_port}" -servername "${host_name}" \
  -CAfile "${ca_file}" -tls1_2 -brief </dev/null >"${work_dir}/tls12.log" 2>&1; then
  report FAIL tls12-refused "a TLS 1.2-only client completed a handshake"
else
  report PASS tls12-refused "TLS 1.2-only handshake refused"
fi

# --- ping --------------------------------------------------------------------------
curl_common=(
  --silent --show-error --tlsv1.3 --cacert "${ca_file}" --max-time 15
  "${curl_resolve[@]}"
)

ping_body="${work_dir}/ping.json"
before_ms="$(python3 -c 'import time; print(int(time.time() * 1000))')"
ping_status="$(curl "${curl_common[@]}" --output "${ping_body}" --write-out '%{http_code}' \
  -H @<(printf 'Authorization: Bearer %s\n' "${api_key}") "${ping_url}" 2>"${work_dir}/ping.err" \
  || true)"
after_ms="$(python3 -c 'import time; print(int(time.time() * 1000))')"
unset api_key

# Prints "<missing fields or OK>\t<skew seconds or ->".
ping_summary="$(
  python3 - "${ping_body}" "${before_ms}" "${after_ms}" <<'EOF' 2>/dev/null || printf 'unreadable\t-'
import json
import sys
from datetime import datetime, timezone

path, before_ms, after_ms = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
try:
    payload = json.load(open(path, encoding="utf-8"))
except (OSError, ValueError):
    print("not JSON\t-")
    sys.exit(0)
missing = [key for key in ("api_version", "server_time", "limits") if key not in payload]
skew = "-"
text = payload.get("server_time")
if isinstance(text, str):
    try:
        server = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if server.tzinfo is None:
            server = server.replace(tzinfo=timezone.utc)
        local = (before_ms + after_ms) / 2000
        skew = f"{server.timestamp() - local:+.3f}"
    except ValueError:
        missing.append("server_time (unparseable)")
print(("OK" if not missing else "missing " + ", ".join(missing)) + "\t" + skew)
EOF
)"
ping_fields="${ping_summary%%$'\t'*}"
skew="${ping_summary##*$'\t'}"

if [[ "${ping_status}" == "200" && "${ping_fields}" == "OK" ]]; then
  report PASS ping "200 with api_version, server_time and limits"
elif [[ "${ping_status}" == "200" ]]; then
  report FAIL ping "200 but ${ping_fields}"
else
  detail="HTTP ${ping_status:-000}"
  [[ -s "${work_dir}/ping.err" ]] && detail+=": $(head -c 200 "${work_dir}/ping.err")"
  report FAIL ping "${detail}"
fi

no_key_status="$(curl "${curl_common[@]}" --output /dev/null --write-out '%{http_code}' \
  "${ping_url}" 2>/dev/null || true)"
if [[ "${no_key_status}" == "401" ]]; then
  report PASS ping-no-key "401 without the key"
else
  report FAIL ping-no-key "HTTP ${no_key_status:-000} without the key, expected 401"
fi

plain_status="$(curl --silent --output /dev/null --write-out '%{http_code}' --max-time 15 \
  "${curl_resolve[@]}" "${plain_ping_url}" 2>/dev/null || true)"
if [[ "${plain_status}" == "403" ]]; then
  report PASS plain-http "403 on ${plain_ping_url}"
else
  report FAIL plain-http "HTTP ${plain_status:-000} on ${plain_ping_url}, expected 403"
fi

# --- time --------------------------------------------------------------------------
if [[ "${skew}" == "-" ]]; then
  report FAIL clock-skew "no usable server_time from ping"
elif python3 -c 'import sys; sys.exit(0 if abs(float(sys.argv[1])) < float(sys.argv[2]) else 1)' \
  "${skew}" "${max_skew}"; then
  report PASS clock-skew "server_time - local clock = ${skew}s (limit ${max_skew}s)"
else
  report FAIL clock-skew "server_time - local clock = ${skew}s (limit ${max_skew}s); check NTP"
fi

if [[ "${ntp_status}" != "true" ]]; then
  report SKIP ntp-synced "disabled with --no-ntp-status"
elif ! command -v timedatectl >/dev/null 2>&1; then
  report SKIP ntp-synced "timedatectl not available"
else
  synced="$(timedatectl show -p NTPSynchronized --value 2>/dev/null || true)"
  if [[ "${synced}" == "yes" ]]; then
    report PASS ntp-synced "timedatectl: NTPSynchronized=yes"
  else
    report FAIL ntp-synced "timedatectl: NTPSynchronized=${synced:-unknown}"
  fi
fi

printf 'RESULT: %s\n' "${overall}"
[[ "${overall}" == "PASS" ]]
