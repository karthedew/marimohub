#!/usr/bin/env bash
#
# Fetches one Runtime's Notebook source from the internal API and installs it
# atomically at OUTPUT_FILE. This script is the entire fetcher image; it
# deliberately never installs or resolves a package at Pod startup -- the
# Runtime images it hands off to are pinned at build time for exactly that
# reason, and a fetcher that shelled out to a package manager would undercut
# that guarantee for itself.
#
# Exit codes are a deliberate, stable contract: the operator classifies this
# init container's failure by its exit code rather than by parsing output.
# curl's own --fail flag collapses every HTTP error class (401, 404, 500...)
# into the same exit status (22), so this script never passes --fail. It
# always captures the response body and the HTTP status code from a single
# curl invocation, then maps that status -- or a transport-level curl
# failure that never produced a status at all -- onto exactly one of:
#
#   0  success: the source was fetched and installed at OUTPUT_FILE
#   10 authentication failure (HTTP 401 or 403)      -- deterministic, no retry
#   11 source not found (HTTP 404)                   -- deterministic, no retry
#   12 response exceeded MAX_SOURCE_BYTES             -- deterministic, no retry
#   13 TLS/CA validation failure                      -- deterministic, no retry
#   20 transient failure (connect/DNS/timeout, or a 5xx that persisted across
#      curl's own retries)                            -- caller should retry
#   30 unexpected HTTP status this script does not otherwise classify
#   40 fetcher misconfiguration: a required env var or mounted file is
#      missing -- a Pod/chart wiring bug, not a Runtime-specific problem
set -u -o pipefail

fail() {
	echo "fetch-source: $2" >&2
	exit "$1"
}

if [ -z "${INTERNAL_API_URL:-}" ]; then
	fail 40 "INTERNAL_API_URL is required"
fi
if [ -z "${RUNTIME_ID:-}" ]; then
	fail 40 "RUNTIME_ID is required"
fi

output_file="${OUTPUT_FILE:-/work/notebook.py}"
credential_file="${RUNTIME_CREDENTIAL_FILE:-/var/run/secrets/marimohub/credential}"
ca_cert_file="${CA_CERT_FILE:-/var/run/secrets/marimohub/ca.crt}"
connect_timeout_seconds="${CONNECT_TIMEOUT_SECONDS:-5}"
total_timeout_seconds="${TOTAL_TIMEOUT_SECONDS:-30}"
retry_count="${RETRY_COUNT:-5}"
# 10485760 (10MiB) mirrors the chart's future maxSourceBytes default. There
# is no chart yet to source it from, so it is hardcoded here and made
# overridable through MAX_SOURCE_BYTES precisely so that when the chart
# exists, wiring its value through means setting one env var on this
# container -- never editing this script.
max_source_bytes="${MAX_SOURCE_BYTES:-10485760}"

if [ ! -r "$credential_file" ]; then
	fail 40 "credential file $credential_file is not readable"
fi
if [ ! -r "$ca_cert_file" ]; then
	fail 40 "CA certificate file $ca_cert_file is not readable"
fi

credential="$(cat "$credential_file")"
if [ -z "$credential" ]; then
	fail 40 "credential file $credential_file is empty"
fi

tmp="${output_file}.tmp"
url="${INTERNAL_API_URL%/}/api/internal/runtimes/${RUNTIME_ID}/source"

http_code="$(curl --show-error --silent \
	--connect-timeout "$connect_timeout_seconds" \
	--max-time "$total_timeout_seconds" \
	--retry "$retry_count" \
	--retry-connrefused \
	--max-filesize "$max_source_bytes" \
	--cacert "$ca_cert_file" \
	-H "Authorization: Bearer ${credential}" \
	-w '%{http_code}' \
	-o "$tmp" \
	"$url")"
curl_exit=$?

if [ "$curl_exit" -ne 0 ]; then
	rm -f "$tmp"
	case "$curl_exit" in
	60) fail 13 "TLS certificate validation failed against $ca_cert_file" ;;
	63) fail 12 "response exceeded the ${max_source_bytes}-byte limit" ;;
	*) fail 20 "transient failure before a complete response was received (curl exit $curl_exit)" ;;
	esac
fi

case "$http_code" in
200)
	# --max-filesize does not reliably bound a chunked-encoded response on
	# every curl version, so the limit is re-checked against what actually
	# landed on disk before the atomic rename makes it visible to marimo.
	actual_bytes="$(stat -c%s "$tmp")"
	if [ "$actual_bytes" -gt "$max_source_bytes" ]; then
		rm -f "$tmp"
		fail 12 "response was ${actual_bytes} bytes, exceeding the ${max_source_bytes}-byte limit"
	fi
	chmod 0660 "$tmp"
	mv "$tmp" "$output_file"
	echo "fetch-source: wrote ${actual_bytes} bytes to $output_file"
	exit 0
	;;
401 | 403)
	rm -f "$tmp"
	fail 10 "authentication failed (HTTP $http_code)"
	;;
404)
	rm -f "$tmp"
	fail 11 "source not found (HTTP $http_code)"
	;;
5??)
	rm -f "$tmp"
	fail 20 "transient server error (HTTP $http_code)"
	;;
*)
	rm -f "$tmp"
	fail 30 "unexpected HTTP status $http_code"
	;;
esac
