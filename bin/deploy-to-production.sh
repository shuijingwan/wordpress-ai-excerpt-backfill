#!/usr/bin/env bash
set -euo pipefail

SSH_ALIAS="aliyun"
REMOTE_PHP="/usr/local/php/bin/php"
REMOTE_ROOT="/root/tools/wordpress-ai-excerpt-backfill"
REMOTE_BIN="${REMOTE_ROOT}/bin"
REMOTE_DATA="${REMOTE_ROOT}/data"
REMOTE_RAW="${REMOTE_DATA}/raw"
REMOTE_LOGS="${REMOTE_ROOT}/logs"
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
LOCAL_EXPORTER="${SCRIPT_DIR}/export-readonly.php"
REMOTE_EXPORTER="${REMOTE_BIN}/export-readonly.php"

usage() {
    printf '%s\n' "Usage:" \
        "  bin/deploy-to-production.sh --dry-run" \
        "  bin/deploy-to-production.sh --deploy" "" \
        "--dry-run prints the plan without connecting." \
        "--deploy authorizes deployment only; it never runs an export."
}

die() { printf 'ERROR: %s\n' "$1" >&2; exit 2; }

mode=""
if (( $# == 0 )); then
    usage >&2
    exit 2
fi
while (( $# > 0 )); do
    case "$1" in
        --deploy)
            [[ -z "${mode}" ]] || die "choose exactly one of --deploy or --dry-run"
            mode="deploy"
            ;;
        --dry-run)
            [[ -z "${mode}" ]] || die "choose exactly one of --deploy or --dry-run"
            mode="dry-run"
            ;;
        --help) usage; exit 0 ;;
        *) die "unknown argument: $1" ;;
    esac
    shift
done
[[ -n "${mode}" ]] || die "--deploy is required for deployment"
[[ -f "${LOCAL_EXPORTER}" ]] || die "local exporter not found: ${LOCAL_EXPORTER}"

if [[ "${mode}" == "dry-run" ]]; then
    printf 'DRY RUN: no network connection will be made.\n'
    printf 'Local file: %s\n' "${LOCAL_EXPORTER}"
    printf 'SSH alias: %s\n' "${SSH_ALIAS}"
    printf 'Remote root: %s\n' "${REMOTE_ROOT}"
    printf 'Remote file: %s\n' "${REMOTE_EXPORTER}"
    printf 'Remote PHP: %s\n' "${REMOTE_PHP}"
    printf 'Remote directories (mode 0700):\n'
    printf '  %s\n' "/root/tools" "${REMOTE_ROOT}" "${REMOTE_BIN}" "${REMOTE_DATA}" "${REMOTE_RAW}" "${REMOTE_LOGS}"
    printf 'Remote PHP mode: 0600\n'
    printf 'Authorized actions: create directories, upload .new, %s -l, chmod, atomic mv, compare SHA-256.\n' "${REMOTE_PHP}"
    exit 0
fi

[[ "${mode}" == "deploy" ]] || die "--deploy is required for deployment"
local_sha256="$(sha256sum "${LOCAL_EXPORTER}" | awk '{print $1}')"
remote_temporary="${REMOTE_EXPORTER}.new.$$"
remote_cleanup_required=0
cleanup_remote_temporary() {
    if (( remote_cleanup_required )); then
        ssh "${SSH_ALIAS}" "rm -f -- '${remote_temporary}'" >/dev/null 2>&1 || true
    fi
}
trap cleanup_remote_temporary EXIT

ssh "${SSH_ALIAS}" "set -euo pipefail
[[ -x \"${REMOTE_PHP}\" ]] || { printf 'ERROR: remote PHP is missing or not executable: ${REMOTE_PHP}\n' >&2; exit 1; }
mkdir -p -- '/root/tools' '${REMOTE_ROOT}' '${REMOTE_BIN}' '${REMOTE_DATA}' '${REMOTE_RAW}' '${REMOTE_LOGS}'
chmod 0700 -- '/root/tools' '${REMOTE_ROOT}' '${REMOTE_BIN}' '${REMOTE_DATA}' '${REMOTE_RAW}' '${REMOTE_LOGS}'
if [[ -f '${REMOTE_EXPORTER}' ]]; then
    printf 'Existing remote SHA-256: '
    sha256sum '${REMOTE_EXPORTER}' | awk '{print \$1}'
else
    printf 'Existing remote SHA-256: none\n'
fi"

remote_cleanup_required=1
scp -O -- "${LOCAL_EXPORTER}" "${SSH_ALIAS}:${remote_temporary}"
remote_sha256="$(ssh "${SSH_ALIAS}" "set -euo pipefail
trap 'rm -f -- \"${remote_temporary}\"' EXIT
\"${REMOTE_PHP}\" -l '${remote_temporary}' >&2
chmod 0600 -- '${remote_temporary}'
uploaded_sha256=\"\$(sha256sum '${remote_temporary}' | awk '{print \$1}')\"
[[ \"\${uploaded_sha256}\" == '${local_sha256}' ]] || { printf 'ERROR: uploaded SHA-256 differs from local file.\n' >&2; exit 1; }
mv -f -- '${remote_temporary}' '${REMOTE_EXPORTER}'
trap - EXIT
sha256sum '${REMOTE_EXPORTER}' | awk '{print \$1}'")"
remote_cleanup_required=0
trap - EXIT

printf 'Local SHA-256:  %s\n' "${local_sha256}"
printf 'Remote SHA-256: %s\n' "${remote_sha256}"
[[ "${local_sha256}" == "${remote_sha256}" ]] || die "local and remote SHA-256 values differ"
printf 'Deployment complete. No export was executed.\n'
