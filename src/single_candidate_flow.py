"""Fixed single-candidate excerpt-save and SlyTranslate orchestration."""

from datetime import datetime, timezone
import json
from pathlib import Path

from src.analyzer import analyze_content
from src.candidate_execution import (SafetyError, authorize_live_selection, backup_record,
    ExcerptValidationError, MAX_EXCERPT_ATTEMPTS, sha256_text, validate_generated_excerpt,
    validate_live, write_backup, write_execution_state, write_rejected_excerpt)
from src.eligibility import evaluate_phase1_eligibility
from src.excerpt_content import extract_excerpt_source


def raw_field(post, name):
    value = post.get(name)
    if isinstance(value, dict):
        value = value.get("raw")
    return value if isinstance(value, str) else ""


def validate_polylang(row, result):
    zh_id = int(row["chinese_post_id"]); en_id = int(row["english_post_id"])
    expected = {
        "chinese_post_id": zh_id, "chinese_language": "zh",
        "linked_english_post_id": en_id, "english_post_id": en_id,
        "english_language": "en", "linked_chinese_post_id": zh_id,
    }
    if not isinstance(result, dict):
        raise SafetyError("Polylang check did not return an object")
    for field, value in expected.items():
        if result.get(field) != value:
            raise SafetyError(f"Polylang check mismatch: {field}")
    return result


def build_live(row, chinese, english, polylang, config):
    zh_id = int(row["chinese_post_id"]); en_id = int(row["english_post_id"])
    if chinese.get("id") != zh_id or english.get("id") != en_id:
        raise SafetyError("WordPress returned an unexpected post ID")
    title = raw_field(chinese, "title"); content = raw_field(chinese, "content")
    excerpt = raw_field(chinese, "excerpt")
    analysis = analyze_content(content, config)
    eligibility = evaluate_phase1_eligibility({
        "post_type": "post", "post_status": chinese.get("status"),
        "language_source": "polylang", "language": "zh",
    }, analysis)
    block_counts = analysis["blocks"]["counts"]
    polylang = validate_polylang(row, polylang)
    return {
        "chinese_exists": True, "chinese_status": chinese.get("status"),
        "chinese_language": polylang["chinese_language"],
        "chinese_excerpt_empty": not excerpt.strip(),
        "chinese_content_sha256": sha256_text(content),
        "is_gutenberg": analysis["blocks"]["has_block_comments"],
        "has_code_block_pro": block_counts.get("kevinbatdorf/code-block-pro", 0) > 0,
        "phase1_eligible": eligibility["eligible"],
        "linked_english_post_id": polylang["linked_english_post_id"],
        "english_status": english.get("status"),
        "english_title_sha256": sha256_text(raw_field(english, "title")),
        "english_excerpt_sha256": sha256_text(raw_field(english, "excerpt")),
        "english_content_sha256": sha256_text(raw_field(english, "content")),
        "chinese_title": title, "chinese_content": content, "chinese_excerpt": excerpt,
        "english_title": raw_field(english, "title"),
        "english_excerpt": raw_field(english, "excerpt"),
        "english_content": raw_field(english, "content"),
    }


def reconcile_translation_started(row, chinese, english):
    """Validate immutable/current fields; return whether translation appears complete."""
    zh_id = int(row["chinese_post_id"]); en_id = int(row["english_post_id"])
    checks = (
        (chinese.get("id") == zh_id, "chinese_id"),
        (english.get("id") == en_id, "english_id"),
        (chinese.get("status") == "publish", "chinese_status"),
        (english.get("status") == "publish", "english_status"),
        (raw_field(chinese, "title") == row["chinese_title"], "chinese_title"),
        (sha256_text(raw_field(chinese, "content")) == row["chinese_content_sha256"],
         "chinese_content_sha256"),
        (bool(raw_field(chinese, "excerpt").strip()), "chinese_excerpt_empty"),
        (bool(raw_field(english, "title").strip()), "english_title_empty"),
        (bool(raw_field(english, "content").strip()), "english_content_empty"),
    )
    failures = [name for passed, name in checks if not passed]
    if failures:
        raise SafetyError("translation_started reconciliation failed: " + ",".join(failures))
    return bool(raw_field(english, "excerpt").strip())


def preflight_live_result(row, wp, polylang_checker, config, resume=False):
    """Perform exactly two GETs and return metadata/hashes only, never post text."""
    zh_id = int(row["chinese_post_id"]); en_id = int(row["english_post_id"])
    chinese = wp.get_post(zh_id)
    english = wp.get_post(en_id)
    polylang_error = None
    try:
        polylang = polylang_checker.check(zh_id, en_id)
    except SafetyError as error:
        polylang = None
        polylang_error = str(error)
    title = raw_field(chinese, "title"); content = raw_field(chinese, "content")
    zh_excerpt = raw_field(chinese, "excerpt")
    en_title = raw_field(english, "title"); en_excerpt = raw_field(english, "excerpt")
    en_content = raw_field(english, "content")
    raw_presence = {
        name: isinstance(chinese.get(name), dict) and isinstance(chinese[name].get("raw"), str)
        for name in ("title", "content", "excerpt")
    }
    language = {"name": "lang", "value": chinese.get("lang")} if "lang" in chinese else None
    relation = None
    if "translations" in chinese:
        mapping = chinese.get("translations")
        relation = {"name": "translations", "english_id": None}
        if isinstance(mapping, dict) and "en" in mapping:
            value = mapping["en"]
            if isinstance(value, dict):
                value = value.get("id")
            try:
                relation["english_id"] = int(value)
            except (TypeError, ValueError):
                pass
    analysis = analyze_content(content, config)
    polylang_confirmed = bool(polylang
        and polylang["chinese_language"] == "zh"
        and polylang["linked_english_post_id"] == en_id
        and polylang["english_language"] == "en"
        and polylang["linked_chinese_post_id"] == zh_id)
    eligibility = evaluate_phase1_eligibility({
        "post_type": "post", "post_status": chinese.get("status"),
        "language_source": "polylang" if polylang_confirmed else None,
        "language": "zh" if polylang_confirmed else None,
    }, analysis)
    block_counts = analysis["blocks"]["counts"]
    code_block_pro_count = block_counts.get("kevinbatdorf/code-block-pro", 0)
    syntaxhighlighter_count = analysis["syntaxhighlighter_count"]
    expected_code_block_pro_count = row.get("expected_code_block_pro_count")
    expected_syntaxhighlighter_count = row.get("expected_syntaxhighlighter_count")
    checks = {
        "returned_ids_match": chinese.get("id") == zh_id and english.get("id") == en_id,
        "statuses_publish": chinese.get("status") == "publish" and english.get("status") == "publish",
        "required_raw_fields_present": all(raw_presence.values()),
        "chinese_language_confirmed": bool(polylang and polylang["chinese_language"] == "zh"),
        "english_language_confirmed": bool(polylang and polylang["english_language"] == "en"),
        "polylang_relation_confirmed": bool(polylang and polylang["linked_english_post_id"] == en_id),
        "polylang_reverse_relation_confirmed": bool(
            polylang and polylang["linked_chinese_post_id"] == zh_id),
        "chinese_excerpt_empty": not zh_excerpt.strip(),
        "chinese_title_matches": title == row["chinese_title"],
        "chinese_content_sha256_matches": sha256_text(content) == row["chinese_content_sha256"],
        "english_title_sha256_matches": sha256_text(en_title) == row["english_title_sha256"],
        "english_excerpt_sha256_matches": sha256_text(en_excerpt) == row["english_excerpt_sha256"],
        "english_content_sha256_matches": sha256_text(en_content) == row["english_content_sha256"],
        "gutenberg": analysis["blocks"]["has_block_comments"],
        "code_block_pro": code_block_pro_count > 0,
        "expected_code_block_pro_count": (
            expected_code_block_pro_count in (None, "")
            or code_block_pro_count == int(expected_code_block_pro_count)
        ),
        "expected_syntaxhighlighter_count": (
            expected_syntaxhighlighter_count in (None, "")
            or syntaxhighlighter_count == int(expected_syntaxhighlighter_count)
        ),
        "phase1_eligible": eligibility["eligible"],
    }
    if resume:
        for name in (
                "chinese_excerpt_empty", "english_title_sha256_matches",
                "english_excerpt_sha256_matches",
                "english_content_sha256_matches"):
            checks[name] = True
    return {
        "mode": "preflight-live", "chinese_post_id": zh_id, "english_post_id": en_id,
        "returned_ids": {"chinese_correct": chinese.get("id") == zh_id,
                         "english_correct": english.get("id") == en_id},
        "statuses": {"chinese": chinese.get("status"), "english": english.get("status")},
        "chinese_response_fields": sorted(chinese.keys()),
        "chinese_raw_fields_present": {f"{key}.raw": value for key, value in raw_presence.items()},
        "chinese_language_field": language, "polylang_relation_field": relation,
        "polylang_check": ({
            "chinese_language": polylang["chinese_language"],
            "linked_english_post_id": polylang["linked_english_post_id"],
            "english_language": polylang["english_language"],
            "linked_chinese_post_id": polylang["linked_chinese_post_id"],
        } if polylang else {"error": polylang_error}),
        "chinese_excerpt_empty": checks["chinese_excerpt_empty"],
        "chinese_title_matches": checks["chinese_title_matches"],
        "english_excerpt_empty": not en_excerpt.strip(),
        "sha256_matches": {
            "chinese_content": checks["chinese_content_sha256_matches"],
            "english_title": checks["english_title_sha256_matches"],
            "english_excerpt": checks["english_excerpt_sha256_matches"],
            "english_content": checks["english_content_sha256_matches"],
        },
        "structure": {"gutenberg": checks["gutenberg"],
                      "code_block_pro": checks["code_block_pro"],
                      "code_block_pro_count": code_block_pro_count,
                      "syntaxhighlighter_count": syntaxhighlighter_count,
                      "expected_code_block_pro_count_matches":
                          checks["expected_code_block_pro_count"],
                      "expected_syntaxhighlighter_count_matches":
                          checks["expected_syntaxhighlighter_count"],
                      "phase1_eligible": checks["phase1_eligible"]},
        "request_counts": {"wordpress_get": 2, "ssh_readonly": 1,
                           "post": 0, "glm": 0, "translation": 0},
        "preflight_passed": all(checks.values()),
    }


class SingleCandidateFlow:
    def __init__(self, manifest_rows, wp, glm, translator, polylang_checker, backup_dir, config,
                 expected_candidate_count=42):
        self.rows = manifest_rows; self.wp = wp; self.glm = glm; self.translator = translator
        self.polylang_checker = polylang_checker
        self.backup_dir = Path(backup_dir); self.config = config
        self.expected_candidate_count = expected_candidate_count

    def _row(self, post_id):
        authorize_live_selection(
            self.rows, [post_id], expected_count=self.expected_candidate_count
        )
        return next(row for row in self.rows if int(row["chinese_post_id"]) == int(post_id))

    def _state_path(self, post_id):
        return self.backup_dir / f"chinese-{int(post_id)}.execution.json"

    def _save_state(self, state, status, **values):
        state.update(values); state["status"] = status
        write_execution_state(self._state_path(state["chinese_post_id"]), state)

    def execute(self, post_id, resume=False):
        row = self._row(post_id); zh_id = int(row["chinese_post_id"]); en_id = int(row["english_post_id"])
        chinese = self.wp.get_post(zh_id); english = self.wp.get_post(en_id)
        initial_polylang = validate_polylang(row, self.polylang_checker.check(zh_id, en_id))
        live = build_live(row, chinese, english, initial_polylang, self.config)
        state_path = self._state_path(zh_id)

        if resume:
            if not state_path.is_file():
                raise SafetyError("resume state does not exist")
            state = json.loads(state_path.read_text(encoding="utf-8"))
            if state.get("status") not in {
                    "excerpt_generated", "chinese_excerpt_saved",
                    "translation_started", "translation_failed"}:
                raise SafetyError("execution state cannot resume from translation stage")
            if state["status"] == "translation_started":
                expected_excerpt = validate_generated_excerpt(state.get("generated_excerpt"))
                if raw_field(chinese, "excerpt") != expected_excerpt:
                    raise SafetyError(
                        "translation_started reconciliation failed: chinese_excerpt_changed")
                translation_complete = reconcile_translation_started(row, chinese, english)
                if translation_complete:
                    # Confirm the bidirectional relation again immediately before convergence.
                    validate_polylang(row, self.polylang_checker.check(zh_id, en_id))
                    self._save_state(
                        state, "completed",
                        completed_at=datetime.now(timezone.utc).isoformat(),
                        translated_post_id=en_id,
                    )
                    return state
            excerpt = validate_generated_excerpt(state.get("generated_excerpt"))
            if raw_field(chinese, "excerpt") != excerpt:
                raise SafetyError("saved Chinese excerpt differs from resume state")
            # Resume deliberately does not require the original excerpt-empty check.
            live["chinese_excerpt_empty"] = True
            failures = validate_live(row, live, resume=True)
            if failures:
                raise SafetyError("resume live validation failed: " + ",".join(failures))
        else:
            failures = validate_live(row, live)
            if failures:
                raise SafetyError("live validation failed: " + ",".join(failures))
            if self.glm is None:
                raise SafetyError("GLM client is required for non-resume execution")
            now = datetime.now(timezone.utc).isoformat()
            backup_path = self.backup_dir / f"chinese-{zh_id}.pre-write.json"
            prior = json.loads(state_path.read_text(encoding="utf-8")) if state_path.is_file() else None
            if prior is not None:
                if (prior.get("status") not in {
                            "prepared", "excerpt_rejected", "excerpt_generated"}
                        or prior.get("chinese_post_id") != zh_id
                        or prior.get("english_post_id") != en_id
                        or not backup_path.is_file()):
                    raise SafetyError("existing execution state requires --resume or manual review")
            else:
                record = backup_record(row, live, executed_at=now, model="glm-4.7", status="prepared")
                backup_path = write_backup(self.backup_dir, record)
            state = {"schema_version": 1, "chinese_post_id": zh_id, "english_post_id": en_id,
                     "backup_path": str(backup_path), "started_at": now, "status": "prepared"}
            self._save_state(state, "prepared")
            cleaned_content = extract_excerpt_source(live["chinese_content"])
            rejected_paths = []
            excerpt = None
            for attempt in range(1, MAX_EXCERPT_ATTEMPTS + 1):
                try:
                    excerpt = self.glm.generate(live["chinese_title"], cleaned_content)
                    excerpt = validate_generated_excerpt(excerpt)
                except ExcerptValidationError as error:
                    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
                    rejected_path = write_rejected_excerpt(
                        self.backup_dir, zh_id, error.raw_excerpt, attempt, timestamp)
                    rejected_paths.append(str(rejected_path))
                    if attempt == MAX_EXCERPT_ATTEMPTS:
                        rejected_state = {
                            "status": "excerpt_rejected", "chinese_post_id": zh_id,
                            "english_post_id": en_id, "error": str(error),
                            "attempts": attempt, "rejected_excerpt_paths": rejected_paths,
                        }
                        write_execution_state(state_path, rejected_state)
                        error.rejected_excerpt_paths = list(rejected_paths)
                        raise
                    continue
                break
            self._save_state(state, "excerpt_generated", generated_excerpt=excerpt,
                             excerpt_attempts=attempt)
            self.wp.update_excerpt(zh_id, excerpt)
            saved = self.wp.get_post(zh_id)
            if (saved.get("id") != zh_id or saved.get("status") != "publish"
                    or raw_field(saved, "excerpt") != excerpt
                    or sha256_text(raw_field(saved, "title")) != sha256_text(live["chinese_title"])
                    or sha256_text(raw_field(saved, "content")) != row["chinese_content_sha256"]):
                raise SafetyError("Chinese excerpt save verification failed")
            self._save_state(state, "chinese_excerpt_saved")

        # Recheck immediately before the only English mutation endpoint.
        validate_polylang(row, self.polylang_checker.check(zh_id, en_id))
        self._save_state(state, "translation_started")
        try:
            response = self.translator.overwrite(zh_id, en_id)
        except Exception as error:
            state.pop("error_response_excerpt", None)
            diagnostics = {"error": str(error),
                           "error_response": getattr(error, "response", None)}
            excerpt = getattr(error, "response_excerpt", None)
            if isinstance(excerpt, str) and excerpt:
                diagnostics["error_response_excerpt"] = excerpt[:500]
            self._save_state(state, "translation_failed", **diagnostics)
            raise
        english_after = self.wp.get_post(en_id)
        # Final relation check does not fetch article bodies again.
        try:
            validate_polylang(row, self.polylang_checker.check(zh_id, en_id))
        except SafetyError as error:
            self._save_state(state, "translation_failed", error=str(error))
            raise
        english_excerpt = raw_field(english_after, "excerpt")
        if (english_after.get("id") != en_id or response.get("translated_post_id") != en_id
                or english_after.get("status") != "publish"
                or not raw_field(english_after, "title").strip()
                or not raw_field(english_after, "content").strip()
                or not english_excerpt.strip()
                or sha256_text(english_excerpt) == sha256_text("")):
            error = "English post verification failed"
            self._save_state(state, "translation_failed", error=error)
            raise SafetyError(error)
        self._save_state(state, "completed", completed_at=datetime.now(timezone.utc).isoformat(),
                         translated_post_id=en_id)
        return state
