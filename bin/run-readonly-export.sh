#!/usr/bin/env bash
set -euo pipefail

SSH_ALIAS="aliyun"
REMOTE_PATH="/usr/local/php/bin:/usr/local/bin:/usr/bin:/bin"
REMOTE_WP="/usr/local/bin/wp"
REMOTE_PYTHON="/usr/bin/python3"
REMOTE_ROOT="/root/tools/wordpress-ai-excerpt-backfill"
REMOTE_EXPORTER="${REMOTE_ROOT}/bin/export-readonly.php"
REMOTE_RAW="${REMOTE_ROOT}/data/raw"
WORDPRESS_PATH="/data/wwwroot/www.shuijingwanwq.com"
WORDPRESS_URL="https://www.shuijingwanwq.com"
MAX_EXPORT_LIMIT=100
DEFAULT_AFTER_ID=0
DEFAULT_BATCH_SIZE=100

usage() {
    printf '%s\n' \
        "Usage: bin/run-readonly-export.sh --limit N [--after-id N] [--batch-size N]" "" \
        "Required: --limit N (1-${MAX_EXPORT_LIMIT}); first production run should use 3 or 5" \
        "Optional: --after-id N; --batch-size N (1-500); --help" "" \
        "This command never deploys code. Run the deployment command separately first."
}
die() { printf 'ERROR: %s\n' "$1" >&2; exit 2; }
is_non_negative_integer() { [[ "$1" =~ ^(0|[1-9][0-9]*)$ ]]; }

limit=""
after_id="${DEFAULT_AFTER_ID}"
batch_size="${DEFAULT_BATCH_SIZE}"
if (( $# == 0 )); then usage >&2; exit 2; fi
while (( $# > 0 )); do
    case "$1" in
        --limit) (( $# >= 2 )) || die "--limit requires a value"; limit="$2"; shift 2 ;;
        --after-id) (( $# >= 2 )) || die "--after-id requires a value"; after_id="$2"; shift 2 ;;
        --batch-size) (( $# >= 2 )) || die "--batch-size requires a value"; batch_size="$2"; shift 2 ;;
        --help) usage; exit 0 ;;
        *) die "unknown argument: $1" ;;
    esac
done
[[ -n "${limit}" ]] || die "--limit is required; unlimited export is not supported"
is_non_negative_integer "${limit}" || die "--limit must be an integer between 1 and ${MAX_EXPORT_LIMIT}"
(( limit >= 1 && limit <= MAX_EXPORT_LIMIT )) || die "--limit must be between 1 and ${MAX_EXPORT_LIMIT}"
is_non_negative_integer "${after_id}" || die "--after-id must be a non-negative integer"
is_non_negative_integer "${batch_size}" || die "--batch-size must be an integer between 1 and 500"
(( batch_size >= 1 && batch_size <= 500 )) || die "--batch-size must be between 1 and 500"

read -r -d '' python_validator <<'PY' || true
import json
from pathlib import Path
import re
import sys
path = Path(sys.argv[1])
limit = int(sys.argv[2])
count = 0
hash_pattern = re.compile(r"^[0-9a-f]{64}$")
required = {"schema_version", "post_type", "post_status", "language_source", "language", "content_sha256"}
with path.open("r", encoding="utf-8") as handle:
    for line_number, line in enumerate(handle, 1):
        if not line.strip():
            raise SystemExit(f"invalid blank JSONL record at line {line_number}")
        try:
            record = json.loads(line)
        except json.JSONDecodeError as error:
            raise SystemExit(f"invalid JSON at line {line_number}: {error}") from error
        if not isinstance(record, dict) or not required <= record.keys():
            raise SystemExit(f"missing required fields at line {line_number}")
        if record["schema_version"] != 1:
            raise SystemExit(f"invalid schema_version at line {line_number}")
        if record["post_type"] != "post" or record["post_status"] != "publish":
            raise SystemExit(f"invalid post scope at line {line_number}")
        if record["language_source"] != "polylang" or record["language"] != "zh":
            raise SystemExit(f"invalid language scope at line {line_number}")
        if not isinstance(record["content_sha256"], str) or not hash_pattern.fullmatch(record["content_sha256"]):
            raise SystemExit(f"invalid content_sha256 at line {line_number}")
        count += 1
if count < 1:
    raise SystemExit("export returned no records")
if count > limit:
    raise SystemExit(f"export returned {count} records, exceeding limit {limit}")
PY

printf -v quoted_validator '%q' "${python_validator}"
printf -v remote_command \
    'set -euo pipefail
remote_path=%q
wp_bin=%q
python_bin=%q
exporter=%q
raw_dir=%q
wordpress_path=%q
wordpress_url=%q
limit=%q
after_id=%q
batch_size=%q
export PATH="$remote_path"
[[ -x "$wp_bin" ]] || { printf "ERROR: WP-CLI is missing or not executable: %%s\\n" "$wp_bin" >&2; exit 2; }
[[ -x "$python_bin" ]] || { printf "ERROR: Python 3 is missing or not executable: %%s\\n" "$python_bin" >&2; exit 2; }
[[ -x /usr/local/php/bin/php ]] || { printf "ERROR: PHP is missing or not executable: /usr/local/php/bin/php\\n" >&2; exit 2; }
[[ -f "$exporter" && ! -L "$exporter" ]] || { printf "ERROR: deployed exporter missing; deploy it first.\\n" >&2; exit 2; }
[[ "$(stat -c %%U "$exporter")" == "root" ]] || { printf "ERROR: deployed exporter must be owned by root.\\n" >&2; exit 2; }
[[ -z "$(find "$exporter" -maxdepth 0 -perm /022 -print -quit)" ]] || { printf "ERROR: exporter must not be group- or world-writable.\\n" >&2; exit 2; }
mkdir -p -- "$raw_dir"
chmod 0700 -- "$raw_dir"
timestamp="$(date -u +%%Y%%m%%dT%%H%%M%%SZ)"
final_file="$raw_dir/wordpress-zh-posts-$timestamp-$$.jsonl"
[[ ! -e "$final_file" ]] || { printf "ERROR: refusing to overwrite remote output.\\n" >&2; exit 2; }
temporary_file="$(mktemp "$raw_dir/.wordpress-zh-posts-$timestamp.XXXXXX.tmp")"
cleanup() { rm -f -- "$temporary_file"; }
trap cleanup EXIT
"$wp_bin" --allow-root eval-file "$exporter" "$limit" "$after_id" "$batch_size" --path="$wordpress_path" --url="$wordpress_url" --skip-themes --quiet > "$temporary_file"
"$python_bin" -c %s "$temporary_file" "$limit"
mv -- "$temporary_file" "$final_file"
trap - EXIT
record_count="$(wc -l < "$final_file")"
byte_count="$(wc -c < "$final_file")"
file_sha256="$(sha256sum "$final_file" | awk '\''{print $1}'\'')"
printf "Remote export saved: %%s\\n" "$final_file"
printf "Records: %%s\\n" "$record_count"
printf "Bytes: %%s\\n" "$byte_count"
printf "SHA-256: %%s\\n" "$file_sha256"' \
    "${REMOTE_PATH}" "${REMOTE_WP}" "${REMOTE_PYTHON}" \
    "${REMOTE_EXPORTER}" "${REMOTE_RAW}" "${WORDPRESS_PATH}" "${WORDPRESS_URL}" \
    "${limit}" "${after_id}" "${batch_size}" "${quoted_validator}"

printf 'Starting bounded remote read-only export. No deployment or download will occur.\n' >&2
ssh "${SSH_ALIAS}" "${remote_command}"
