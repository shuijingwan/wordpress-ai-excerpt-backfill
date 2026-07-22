"""Fixed-manifest safety boundary for excerpt and translation replacement."""

from collections import Counter
import csv
import hashlib
import json
import os
from pathlib import Path
import tempfile
import re


EXPECTED_CANDIDATES = 42
DEFAULT_LIVE_LIMIT = 1
MAX_EXCERPT_ATTEMPTS = 3
CANDIDATE_FIELDS = (
    "chinese_post_id", "chinese_title", "chinese_content_sha256",
    "chinese_excerpt_empty", "english_post_id", "english_post_status",
    "english_title_sha256", "english_excerpt_sha256", "english_content_sha256",
    "candidate_reason", "execution_status",
)
REASON = (
    "published Polylang zh; category=gutenberg-code-block-pro; excerpt_empty=True; "
    "phase1_eligible=True; linked English status=publish"
)


class SafetyError(ValueError):
    """Raised before an AI call or WordPress write can occur."""


class ExcerptValidationError(SafetyError):
    """A rejected GLM message content, without any request or credential data."""

    def __init__(self, message, raw_excerpt):
        super().__init__(message)
        self.raw_excerpt = raw_excerpt
        self.rejected_excerpt_path = None


def sha256_text(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _true(value):
    return str(value).lower() == "true"


def select_inventory_rows(rows):
    """Apply the exact, closed candidate predicate to formal inventory rows."""
    return [row for row in rows if (
        row.get("category") == "gutenberg-code-block-pro"
        and _true(row.get("excerpt_empty"))
        and _true(row.get("phase1_eligible"))
        and _true(row.get("has_english_translation"))
        and row.get("english_post_status") == "publish"
    )]


def load_csv(path):
    with Path(path).open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def load_snapshot(path):
    records = {}
    with Path(path).open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                raise SafetyError(f"snapshot line {line_number} is blank")
            record = json.loads(line)
            post_id = int(record["chinese_post_id"])
            if post_id in records:
                raise SafetyError(f"duplicate snapshot Chinese ID: {post_id}")
            records[post_id] = record
    return records


def validate_manifest(rows, expected_count=EXPECTED_CANDIDATES):
    if len(rows) != expected_count:
        raise SafetyError(f"candidate count must be exactly {expected_count}, got {len(rows)}")
    chinese = [int(row["chinese_post_id"]) for row in rows]
    english = [int(row["english_post_id"]) for row in rows]
    if len(set(chinese)) != len(chinese):
        raise SafetyError("Chinese post IDs must be unique")
    if len(set(english)) != len(english):
        raise SafetyError("English post IDs must be unique")
    for row in rows:
        if not _true(row["chinese_excerpt_empty"]):
            raise SafetyError(f"manifest excerpt is not empty: {row['chinese_post_id']}")
        if row["english_post_status"] != "publish":
            raise SafetyError(f"English post is not published: {row['chinese_post_id']}")
        for field in ("chinese_content_sha256", "english_title_sha256",
                      "english_excerpt_sha256", "english_content_sha256"):
            value = row[field]
            if len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
                raise SafetyError(f"invalid {field}: {row['chinese_post_id']}")
    return True


def validate_live(row, live):
    """Return reasons; never search for a replacement when validation fails."""
    checks = (
        (live.get("chinese_exists") is True, "chinese_missing"),
        (live.get("chinese_status") == "publish", "chinese_not_published"),
        (live.get("chinese_language") == "zh", "chinese_not_polylang_zh"),
        (live.get("chinese_excerpt_empty") is True, "chinese_excerpt_not_empty"),
        (live.get("chinese_content_sha256") == row["chinese_content_sha256"], "chinese_content_changed"),
        (live.get("is_gutenberg") is True, "not_gutenberg"),
        (live.get("has_code_block_pro") is True, "no_code_block_pro"),
        (live.get("phase1_eligible") is True, "phase1_ineligible"),
        (int(live.get("linked_english_post_id") or 0) == int(row["english_post_id"]), "english_relation_changed"),
        (live.get("english_status") == "publish", "english_not_published"),
        (live.get("english_title_sha256") == row["english_title_sha256"], "english_title_changed"),
        (live.get("english_excerpt_sha256") == row["english_excerpt_sha256"], "english_excerpt_changed"),
        (live.get("english_content_sha256") == row["english_content_sha256"], "english_content_changed"),
    )
    return [reason for passed, reason in checks if not passed]


def dry_run(manifest_rows, snapshot_by_id, protected_ids=()):
    validate_manifest(manifest_rows)
    manifest_ids = {int(row["chinese_post_id"]) for row in manifest_rows}
    protected = {int(value) for value in protected_ids}
    if manifest_ids & protected:
        raise SafetyError("non-empty-excerpt protected posts entered the manifest")
    reasons = Counter()
    passed = 0
    for row in manifest_rows:
        post_id = int(row["chinese_post_id"])
        live = snapshot_by_id.get(post_id)
        failures = ["snapshot_missing"] if live is None else validate_live(row, live)
        if failures:
            reasons.update(failures)
        else:
            passed += 1
    return {
        "candidate_count": len(manifest_rows), "passed": passed,
        "skipped": len(manifest_rows) - passed, "skip_reasons": dict(sorted(reasons.items())),
        "one_to_one_ids": len(manifest_ids) == len({int(r["english_post_id"]) for r in manifest_rows}),
        "exactly_42": len(manifest_rows) == EXPECTED_CANDIDATES,
        "protected_46_excluded": len(protected) == 46 and not (manifest_ids & protected),
        "wordpress_writes": 0, "ai_api_calls": 0,
        "ssh_readonly_calls": 0, "translation_calls": 0,
    }


def authorize_live_selection(manifest_rows, requested_ids, batch_authorized=False,
                             expected_count=EXPECTED_CANDIDATES):
    validate_manifest(manifest_rows, expected_count=expected_count)
    allowed = {int(row["chinese_post_id"]) for row in manifest_rows}
    requested = [int(value) for value in requested_ids]
    if not requested or len(set(requested)) != len(requested):
        raise SafetyError("explicit, unique Chinese post IDs are required")
    if not set(requested) <= allowed:
        raise SafetyError("requested ID is outside the fixed manifest")
    if len(requested) > DEFAULT_LIVE_LIMIT and not batch_authorized:
        raise SafetyError("live execution defaults to one post; explicit batch authorization is required")
    return requested


def validate_generated_excerpt(value, minimum=80, maximum=300):
    if not isinstance(value, str) or not value.strip():
        raise ExcerptValidationError("generated Chinese excerpt is empty",
                                     value if isinstance(value, str) else "")
    text = value.strip()
    if not minimum <= len(text) <= maximum:
        raise ExcerptValidationError("generated Chinese excerpt length is invalid", value)
    if "\n" in text or "\r" in text:
        raise ExcerptValidationError("generated Chinese excerpt must be one paragraph", value)
    forbidden = ("<!--", "-->", "```", "[/", "http://", "https://")
    if any(token in text.lower() for token in forbidden):
        raise ExcerptValidationError("generated Chinese excerpt contains forbidden markup or payload", value)
    if re.search(r"<[^>]+>|\[[A-Za-z/][^\]]*\]", text):
        raise ExcerptValidationError("generated Chinese excerpt contains HTML or shortcode markup", value)
    if re.search(r"(^|\s)(?:#{1,6}\s|[-*+]\s|\d+[.)]\s)|[*_]{2}", text):
        raise ExcerptValidationError("generated Chinese excerpt contains Markdown or a list", value)
    return text


def backup_record(row, live, *, executed_at, model=None, request_id=None, status="prepared"):
    """Create a per-post recoverable pre-write record (caller persists atomically)."""
    return {
        "schema_version": 1, "chinese_post_id": int(row["chinese_post_id"]),
        "english_post_id": int(row["english_post_id"]), "executed_at": executed_at,
        "ai_model": model, "ai_request_id": request_id, "status": status,
        "before": {
            "chinese_excerpt": live["chinese_excerpt"],
            "english_title": live["english_title"],
            "english_excerpt": live["english_excerpt"],
            "english_content": live["english_content"],
        },
        "association": {
            "chinese_language": live.get("chinese_language"),
            "linked_english_post_id": int(live.get("linked_english_post_id") or 0),
        },
        "post_status": {
            "chinese": live.get("chinese_status"), "english": live.get("english_status"),
        },
        "sha256": {
            "chinese_excerpt": sha256_text(live["chinese_excerpt"]),
            "chinese_title": sha256_text(live.get("chinese_title", "")),
            "chinese_content": sha256_text(live.get("chinese_content", "")),
            "english_title": sha256_text(live["english_title"]),
            "english_excerpt": sha256_text(live["english_excerpt"]),
            "english_content": sha256_text(live["english_content"]),
        },
    }


def write_backup(directory, record):
    """Atomically persist one private, independently restorable pre-write backup."""
    root = Path(directory)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(root, 0o700)
    post_id = int(record["chinese_post_id"])
    target = root / f"chinese-{post_id}.pre-write.json"
    if target.exists():
        raise SafetyError(f"refusing to overwrite existing backup: {post_id}")
    fd, temporary = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=root)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(record, handle, ensure_ascii=False, indent=2)
            handle.write("\n"); handle.flush(); os.fsync(handle.fileno())
        os.replace(temporary, target)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return target


def write_execution_state(path, record):
    """Atomically replace the non-secret resumable state for one post."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(target.parent, 0o700)
    fd, temporary = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(record, handle, ensure_ascii=False, indent=2)
            handle.write("\n"); handle.flush(); os.fsync(handle.fileno())
        os.replace(temporary, target)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return target


def write_rejected_excerpt(directory, chinese_post_id, raw_excerpt, attempt, timestamp):
    """Atomically store only rejected choices[0].message.content."""
    if (type(chinese_post_id) is not int or chinese_post_id < 1
            or type(attempt) is not int or not 1 <= attempt <= MAX_EXCERPT_ATTEMPTS
            or not isinstance(raw_excerpt, str)):
        raise SafetyError("invalid rejected excerpt record")
    root = Path(directory) / "rejected"
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(root, 0o700)
    target = root / (
        f"chinese-{chinese_post_id}-glm47-rejected-attempt-{attempt}-{timestamp}.txt")
    if target.exists():
        raise SafetyError("refusing to overwrite rejected excerpt")
    fd, temporary = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=root)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(raw_excerpt); handle.flush(); os.fsync(handle.fileno())
        os.replace(temporary, target)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return target


def guarded_pipeline(row, live, generate_excerpt, translate, validate_translation, write_chinese, write_english):
    """Dependency-injected ordering used by a future explicitly authorized live adapter."""
    failures = validate_live(row, live)
    if failures:
        raise SafetyError("live validation failed: " + ",".join(failures))
    excerpt = validate_generated_excerpt(generate_excerpt(live["chinese_title"], live["chinese_content"]))
    translated = translate(live["chinese_title"], excerpt, live["chinese_content"])
    if not validate_translation(translated):
        raise SafetyError("English translation validation failed")
    write_chinese(int(row["chinese_post_id"]), excerpt)
    write_english(int(row["english_post_id"]), translated)
