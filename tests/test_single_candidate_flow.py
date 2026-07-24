import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from src.candidate_execution import ExcerptValidationError, SafetyError
from src.http_json import HttpJsonError
from src.single_candidate_flow import SingleCandidateFlow


ROOT = Path(__file__).resolve().parents[1]
CONFIG = json.loads((ROOT / "config/classification.json").read_text(encoding="utf-8"))
VALID_EXCERPT = (
    "这篇文章说明一个具体技术问题的背景、排查思路和完整操作过程，并根据实际执行结果总结最终结论，"
    "同时保留关键技术名称，避免加入原文没有提及的效果、判断或营销表达，可直接作为博客文章的中文摘要使用。"
)
CONTENT = (
    '<!-- wp:paragraph --><p>这是一段用于生成摘要的正常中文说明文字。</p><!-- /wp:paragraph -->'
    '<!-- wp:kevinbatdorf/code-block-pro -->'
    '<div class="wp-block-kevinbatdorf-code-block-pro"><textarea>x</textarea>'
    '<pre class="shiki"><code><span class="line">x</span></code></pre></div>'
    '<!-- /wp:kevinbatdorf/code-block-pro -->'
)


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def rows():
    result = []
    for index in range(1, 43):
        zh_content = CONTENT if index == 1 else f"zh-{index}"
        en_title = "Old title" if index == 1 else f"title-{index}"
        en_excerpt = "" if index == 1 else f"excerpt-{index}"
        en_content = "Old content" if index == 1 else f"content-{index}"
        result.append({"chinese_post_id": str(index), "chinese_title": "测试标题",
            "chinese_content_sha256": digest(zh_content), "chinese_excerpt_empty": "True",
            "english_post_id": str(1000 + index), "english_post_status": "publish",
            "english_title_sha256": digest(en_title), "english_excerpt_sha256": digest(en_excerpt),
            "english_content_sha256": digest(en_content), "candidate_reason": "fixed",
            "execution_status": "pending"})
    return result


def post(post_id, title, excerpt, content, translations=None, lang=None):
    value = {"id": post_id, "status": "publish", "title": {"raw": title},
             "excerpt": {"raw": excerpt}, "content": {"raw": content}}
    if translations is not None: value["translations"] = translations
    if lang is not None: value["lang"] = lang
    return value


class MockWp:
    def __init__(self):
        # Production REST context=edit has neither lang nor translations.
        self.posts = {1: post(1, "测试标题", "", CONTENT),
                      1001: post(1001, "Old title", "", "Old content")}
        self.update_calls = []; self.get_calls = []

    def get_post(self, post_id):
        self.get_calls.append(post_id); return copy.deepcopy(self.posts[post_id])

    def update_excerpt(self, post_id, excerpt):
        self.update_calls.append((post_id, excerpt)); self.posts[post_id]["excerpt"]["raw"] = excerpt
        return copy.deepcopy(self.posts[post_id])


class MockGlm:
    def __init__(self): self.calls = 0
    def generate(self, title, content): self.calls += 1; return VALID_EXCERPT


class RejectedGlm(MockGlm):
    RAW = "- " + "这是一段包含列表标记的原始模型摘要文本。" * 8
    def generate(self, title, content):
        self.calls += 1
        raise ExcerptValidationError("generated Chinese excerpt contains Markdown or a list", self.RAW)


class SequenceGlm(MockGlm):
    def __init__(self, outcomes, before_call=None):
        super().__init__(); self.outcomes = list(outcomes); self.before_call = before_call
    def generate(self, title, content):
        if self.before_call: self.before_call()
        self.calls += 1
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception): raise outcome
        return outcome


def rejected_error(number):
    return ExcerptValidationError(
        "generated Chinese excerpt contains Markdown or a list",
        f"- 第 {number} 次被拒绝的模型摘要" + "原始文本。" * 20,
    )


class MockTranslator:
    def __init__(self, wp, fail=False, populate_excerpt=True):
        self.wp = wp; self.fail = fail; self.populate_excerpt = populate_excerpt; self.calls = 0
    def overwrite(self, zh_id, en_id):
        self.calls += 1
        if self.fail: raise HttpJsonError("mock translation failed")
        self.wp.posts[en_id]["title"]["raw"] = "Translated title"
        self.wp.posts[en_id]["content"]["raw"] = "Translated content"
        if self.populate_excerpt: self.wp.posts[en_id]["excerpt"]["raw"] = "Translated excerpt"
        return {"source_post_id": zh_id, "translated_post_id": en_id, "target_language": "en",
                "translated_post_type": "post", "post_status": "publish"}


class MockPolylang:
    def __init__(self, results=None):
        self.calls = []
        self.results = list(results or [])
    @staticmethod
    def valid():
        return {"chinese_post_id": 1, "chinese_language": "zh",
                "linked_english_post_id": 1001, "english_post_id": 1001,
                "english_language": "en", "linked_chinese_post_id": 1}
    def check(self, zh_id, en_id):
        self.calls.append((zh_id, en_id))
        return copy.deepcopy(self.results.pop(0) if self.results else self.valid())


class SingleCandidateFlowTest(unittest.TestCase):
    def make_flow(self, directory, wp=None, glm=None, translator=None, polylang=None):
        wp = wp or MockWp(); glm = glm or MockGlm(); translator = translator or MockTranslator(wp)
        polylang = polylang or MockPolylang()
        return (SingleCandidateFlow(rows(), wp, glm, translator, polylang, directory, CONFIG),
                wp, glm, translator, polylang)

    def test_translation_only_after_verified_excerpt_save(self):
        with tempfile.TemporaryDirectory() as directory:
            flow, wp, glm, translator, polylang = self.make_flow(directory)
            state = flow.execute(1)
            self.assertEqual("completed", state["status"]); self.assertEqual(1, translator.calls)
            self.assertEqual(1, state["excerpt_attempts"])
            self.assertEqual(VALID_EXCERPT, wp.posts[1]["excerpt"]["raw"])
            self.assertEqual([(1, 1001)] * 3, polylang.calls)

    def test_failed_excerpt_save_never_calls_translation(self):
        class BrokenWp(MockWp):
            def update_excerpt(self, post_id, excerpt):
                self.update_calls.append((post_id, excerpt)); return self.posts[post_id]
        with tempfile.TemporaryDirectory() as directory:
            wp = BrokenWp(); translator = MockTranslator(wp)
            flow, _, _, _, _ = self.make_flow(directory, wp=wp, translator=translator)
            with self.assertRaisesRegex(SafetyError, "save verification"):
                flow.execute(1)
            self.assertEqual(0, translator.calls)

    def test_translation_failure_resume_does_not_regenerate_excerpt(self):
        with tempfile.TemporaryDirectory() as directory:
            wp = MockWp(); glm = MockGlm(); failing = MockTranslator(wp, fail=True)
            flow, _, _, _, first_polylang = self.make_flow(directory, wp, glm, failing)
            with self.assertRaises(HttpJsonError): flow.execute(1)
            self.assertEqual(1, glm.calls)
            state_path = Path(directory) / "chinese-1.execution.json"
            self.assertEqual("translation_failed", json.loads(state_path.read_text())["status"])
            succeeding = MockTranslator(wp)
            resumed, _, _, _, resume_polylang = self.make_flow(directory, wp, glm, succeeding)
            state = resumed.execute(1, resume=True)
            self.assertEqual("completed", state["status"]); self.assertEqual(1, glm.calls)
            self.assertEqual([(1, 1001)] * 2, first_polylang.calls)
            self.assertEqual([(1, 1001)] * 3, resume_polylang.calls)

    def test_all_translation_resume_states_accept_no_glm_client(self):
        for resume_status in (
                "excerpt_generated", "chinese_excerpt_saved",
                "translation_started", "translation_failed"):
            with self.subTest(status=resume_status), tempfile.TemporaryDirectory() as directory:
                wp = MockWp(); initial_glm = MockGlm()
                initial = SingleCandidateFlow(rows(), wp, initial_glm, MockTranslator(wp, fail=True),
                                              MockPolylang(), directory, CONFIG)
                with self.assertRaises(HttpJsonError): initial.execute(1)
                state_path = Path(directory) / "chinese-1.execution.json"
                state = json.loads(state_path.read_text()); state["status"] = resume_status
                state_path.write_text(json.dumps(state), encoding="utf-8")
                translator = MockTranslator(wp)
                resumed = SingleCandidateFlow(rows(), wp, None, translator, MockPolylang(), directory, CONFIG)
                self.assertEqual("completed", resumed.execute(1, resume=True)["status"])
                self.assertEqual(1, translator.calls)

    def test_translation_started_with_nonempty_english_excerpt_converges_without_translate(self):
        with tempfile.TemporaryDirectory() as directory:
            wp = MockWp(); initial = SingleCandidateFlow(
                rows(), wp, MockGlm(), MockTranslator(wp, fail=True), MockPolylang(), directory, CONFIG)
            with self.assertRaises(HttpJsonError): initial.execute(1)
            state_path = Path(directory) / "chinese-1.execution.json"
            state = json.loads(state_path.read_text()); state["status"] = "translation_started"
            state_path.write_text(json.dumps(state), encoding="utf-8")
            wp.posts[1001]["title"]["raw"] = "Translated title"
            wp.posts[1001]["content"]["raw"] = "Translated content"
            wp.posts[1001]["excerpt"]["raw"] = "Translated excerpt"
            translator = MockTranslator(wp); polylang = MockPolylang()
            resumed = SingleCandidateFlow(rows(), wp, None, translator, polylang, directory, CONFIG)
            result = resumed.execute(1, resume=True)
            self.assertEqual("completed", result["status"])
            self.assertEqual(1001, result["translated_post_id"])
            self.assertIn("completed_at", result)
            self.assertEqual(0, translator.calls)
            self.assertEqual([(1, 1001)] * 2, polylang.calls)

    def test_translation_started_empty_english_excerpt_retries_translate_once(self):
        with tempfile.TemporaryDirectory() as directory:
            wp = MockWp(); initial = SingleCandidateFlow(
                rows(), wp, MockGlm(), MockTranslator(wp, fail=True), MockPolylang(), directory, CONFIG)
            with self.assertRaises(HttpJsonError): initial.execute(1)
            state_path = Path(directory) / "chinese-1.execution.json"
            state = json.loads(state_path.read_text()); state["status"] = "translation_started"
            state_path.write_text(json.dumps(state), encoding="utf-8")
            translator = MockTranslator(wp)
            resumed = SingleCandidateFlow(rows(), wp, None, translator, MockPolylang(), directory, CONFIG)
            self.assertEqual("completed", resumed.execute(1, resume=True)["status"])
            self.assertEqual(1, translator.calls)

    def test_translation_started_bad_polylang_rejects_without_translate(self):
        with tempfile.TemporaryDirectory() as directory:
            wp = MockWp(); initial = SingleCandidateFlow(
                rows(), wp, MockGlm(), MockTranslator(wp, fail=True), MockPolylang(), directory, CONFIG)
            with self.assertRaises(HttpJsonError): initial.execute(1)
            state_path = Path(directory) / "chinese-1.execution.json"
            state = json.loads(state_path.read_text()); state["status"] = "translation_started"
            state_path.write_text(json.dumps(state), encoding="utf-8")
            translator = MockTranslator(wp)
            bad = MockPolylang([MockPolylang.valid() | {"linked_chinese_post_id": 999}])
            resumed = SingleCandidateFlow(rows(), wp, None, translator, bad, directory, CONFIG)
            with self.assertRaisesRegex(SafetyError, "linked_chinese_post_id"):
                resumed.execute(1, resume=True)
            self.assertEqual(0, translator.calls); self.assertEqual([], wp.update_calls[1:])

    def test_final_rest_disconnect_then_resume_converges_without_retranslation(self):
        class DisconnectingWp(MockWp):
            def __init__(self): super().__init__(); self.disconnect_next_english = False
            def get_post(self, post_id):
                if post_id == 1001 and self.disconnect_next_english:
                    self.disconnect_next_english = False
                    raise ConnectionError("RemoteDisconnected")
                return super().get_post(post_id)
        class ArmingTranslator(MockTranslator):
            def overwrite(self, zh_id, en_id):
                result = super().overwrite(zh_id, en_id)
                self.wp.disconnect_next_english = True
                return result
        with tempfile.TemporaryDirectory() as directory:
            wp = DisconnectingWp(); translator = ArmingTranslator(wp)
            flow = SingleCandidateFlow(
                rows(), wp, MockGlm(), translator, MockPolylang(), directory, CONFIG)
            with self.assertRaisesRegex(ConnectionError, "RemoteDisconnected"):
                flow.execute(1)
            state = json.loads((Path(directory) / "chinese-1.execution.json").read_text())
            self.assertEqual("translation_started", state["status"])
            resumed = SingleCandidateFlow(
                rows(), wp, None, translator, MockPolylang(), directory, CONFIG)
            self.assertEqual("completed", resumed.execute(1, resume=True)["status"])
            self.assertEqual(1, translator.calls)

    def test_non_resume_requires_glm_client_before_backup_or_write(self):
        with tempfile.TemporaryDirectory() as directory:
            wp = MockWp(); translator = MockTranslator(wp)
            flow = SingleCandidateFlow(rows(), wp, None, translator, MockPolylang(), directory, CONFIG)
            with self.assertRaisesRegex(SafetyError, "GLM client is required"):
                flow.execute(1)
            self.assertEqual([], wp.update_calls); self.assertEqual(0, translator.calls)
            self.assertFalse((Path(directory) / "chinese-1.pre-write.json").exists())

    def test_resume_rechecks_content_hash_and_relation(self):
        for mutation, reason in (
            (lambda wp: wp.posts[1]["content"].update(raw=CONTENT + "changed"), "chinese_content_changed"),
            (lambda wp: wp.posts[1]["translations"].update(en=1999), "english_relation_changed"),
        ):
            with self.subTest(reason=reason), tempfile.TemporaryDirectory() as directory:
                wp = MockWp(); glm = MockGlm(); flow, _, _, _, _ = self.make_flow(
                    directory, wp, glm, MockTranslator(wp, fail=True))
                with self.assertRaises(HttpJsonError): flow.execute(1)
                if reason == "chinese_content_changed":
                    mutation(wp)
                    resumed, _, _, _, _ = self.make_flow(directory, wp, glm, MockTranslator(wp))
                else:
                    changed = MockPolylang([MockPolylang.valid() | {"linked_english_post_id": 1999}])
                    resumed, _, _, _, _ = self.make_flow(
                        directory, wp, glm, MockTranslator(wp), changed)
                with self.assertRaisesRegex(SafetyError, reason if reason != "english_relation_changed" else "Polylang"):
                    resumed.execute(1, resume=True)
                self.assertEqual(1, glm.calls)

    def test_translation_error_response_is_saved(self):
        class ErrorTranslator(MockTranslator):
            def overwrite(self, zh_id, en_id):
                self.calls += 1
                response = {"code": "swq_full_article_token_validation_failed", "data": None}
                raise HttpJsonError("translation endpoint error", response=response)
        with tempfile.TemporaryDirectory() as directory:
            wp = MockWp(); flow, _, _, _, _ = self.make_flow(directory, wp=wp, translator=ErrorTranslator(wp))
            with self.assertRaises(HttpJsonError): flow.execute(1)
            state = json.loads((Path(directory) / "chinese-1.execution.json").read_text())
            self.assertEqual("swq_full_article_token_validation_failed", state["error_response"]["code"])

    def test_non_json_http_error_saves_only_limited_excerpt(self):
        class HtmlErrorTranslator(MockTranslator):
            def overwrite(self, zh_id, en_id):
                self.calls += 1
                raise HttpJsonError("HTTP request failed with status 500", response=None,
                                    response_excerpt="Server failure " + "X" * 486)
        with tempfile.TemporaryDirectory() as directory:
            wp = MockWp(); flow, _, _, _, _ = self.make_flow(
                directory, wp=wp, translator=HtmlErrorTranslator(wp))
            with self.assertRaisesRegex(HttpJsonError, "status 500"):
                flow.execute(1)
            state_text = (Path(directory) / "chinese-1.execution.json").read_text()
            state = json.loads(state_text)
            self.assertIsNone(state["error_response"])
            self.assertLessEqual(len(state["error_response_excerpt"]), 500)
            self.assertNotIn("<html", state_text)

    def test_empty_english_excerpt_cannot_complete(self):
        with tempfile.TemporaryDirectory() as directory:
            wp = MockWp(); translator = MockTranslator(wp, populate_excerpt=False)
            flow, _, _, _, _ = self.make_flow(directory, wp=wp, translator=translator)
            with self.assertRaisesRegex(SafetyError, "English post verification"):
                flow.execute(1)
            state = json.loads((Path(directory) / "chinese-1.execution.json").read_text())
            self.assertEqual("translation_failed", state["status"])

    def test_backup_and_state_contain_no_credentials(self):
        with tempfile.TemporaryDirectory() as directory:
            flow, _, _, _, _ = self.make_flow(directory)
            flow.execute(1)
            combined = "".join(path.read_text() for path in Path(directory).glob("*.json"))
            for secret in ("ZHIPU_API_KEY", "WP_ADMIN_COOKIE", "WP_REST_NONCE"):
                self.assertNotIn(secret, combined)

    def test_initial_polylang_failure_stops_all_mutations(self):
        with tempfile.TemporaryDirectory() as directory:
            wp = MockWp(); glm = MockGlm(); translator = MockTranslator(wp)
            bad = MockPolylang([MockPolylang.valid() | {"chinese_language": "en"}])
            flow, _, _, _, _ = self.make_flow(directory, wp, glm, translator, bad)
            with self.assertRaisesRegex(SafetyError, "chinese_language"):
                flow.execute(1)
            self.assertEqual(0, glm.calls); self.assertEqual([], wp.update_calls)
            self.assertEqual(0, translator.calls)

    def test_relation_change_after_excerpt_save_stops_translation(self):
        with tempfile.TemporaryDirectory() as directory:
            wp = MockWp(); glm = MockGlm(); translator = MockTranslator(wp)
            bad = MockPolylang([MockPolylang.valid(),
                MockPolylang.valid() | {"linked_english_post_id": 1999}])
            flow, _, _, _, _ = self.make_flow(directory, wp, glm, translator, bad)
            with self.assertRaisesRegex(SafetyError, "linked_english_post_id"):
                flow.execute(1)
            self.assertEqual(1, glm.calls); self.assertEqual(1, len(wp.update_calls))
            self.assertEqual(0, translator.calls)

    def test_relation_change_after_translation_cannot_complete(self):
        with tempfile.TemporaryDirectory() as directory:
            wp = MockWp(); translator = MockTranslator(wp)
            bad = MockPolylang([MockPolylang.valid(), MockPolylang.valid(),
                MockPolylang.valid() | {"english_language": "zh"}])
            flow, _, _, _, _ = self.make_flow(directory, wp=wp, translator=translator, polylang=bad)
            with self.assertRaisesRegex(SafetyError, "english_language"):
                flow.execute(1)
            state = json.loads((Path(directory) / "chinese-1.execution.json").read_text())
            self.assertEqual("translation_failed", state["status"])

    def test_rejected_excerpt_is_private_exact_and_stops_mutations(self):
        with tempfile.TemporaryDirectory() as directory:
            wp = MockWp(); glm = RejectedGlm(); translator = MockTranslator(wp)
            flow, _, _, _, _ = self.make_flow(directory, wp, glm, translator)
            with self.assertRaises(ExcerptValidationError) as raised:
                flow.execute(1)
            paths = [Path(value) for value in raised.exception.rejected_excerpt_paths]
            self.assertEqual(3, len(paths)); self.assertEqual(3, len(set(paths)))
            for attempt, path in enumerate(paths, 1):
                self.assertIn(f"attempt-{attempt}-", path.name)
                self.assertEqual(RejectedGlm.RAW, path.read_text(encoding="utf-8"))
                self.assertEqual(0o600, path.stat().st_mode & 0o777)
                self.assertEqual(0o700, path.parent.stat().st_mode & 0o777)
            state_path = Path(directory) / "chinese-1.execution.json"
            state = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual({"status", "chinese_post_id", "english_post_id", "error",
                              "attempts", "rejected_excerpt_paths"}, set(state))
            self.assertEqual("excerpt_rejected", state["status"])
            self.assertEqual(3, state["attempts"])
            self.assertNotIn(RejectedGlm.RAW, state_path.read_text(encoding="utf-8"))
            for secret in ("API-KEY", "COOKIE-SECRET", "NONCE-SECRET"):
                self.assertTrue(all(secret not in path.read_text(encoding="utf-8") for path in paths))
                self.assertNotIn(secret, state_path.read_text(encoding="utf-8"))
            self.assertEqual([], wp.update_calls); self.assertEqual(0, translator.calls)

    def test_resume_rejects_excerpt_rejected_and_regular_retry_calls_glm_again(self):
        with tempfile.TemporaryDirectory() as directory:
            wp = MockWp(); rejected = RejectedGlm(); translator = MockTranslator(wp)
            flow, _, _, _, _ = self.make_flow(directory, wp, rejected, translator)
            with self.assertRaises(ExcerptValidationError): flow.execute(1)
            resume_translator = MockTranslator(wp)
            resume = SingleCandidateFlow(
                rows(), wp, None, resume_translator, MockPolylang(), directory, CONFIG)
            with self.assertRaisesRegex(SafetyError, "cannot resume"):
                resume.execute(1, resume=True)
            self.assertEqual(0, resume_translator.calls)
            retry_glm = MockGlm()
            retry, _, _, _, _ = self.make_flow(directory, wp, retry_glm, MockTranslator(wp))
            self.assertEqual("completed", retry.execute(1)["status"])
            self.assertEqual(1, retry_glm.calls)

    def test_regular_retry_can_restart_from_excerpt_generated_before_wp_write(self):
        class FailingWriteWp(MockWp):
            def update_excerpt(self, post_id, excerpt):
                raise OSError("local mock write interruption")

        with tempfile.TemporaryDirectory() as directory:
            failed_wp = FailingWriteWp()
            flow, _, _, _, _ = self.make_flow(
                directory, failed_wp, MockGlm(), MockTranslator(failed_wp))
            with self.assertRaises(OSError):
                flow.execute(1)
            state_path = Path(directory) / "chinese-1.execution.json"
            self.assertEqual(
                "excerpt_generated",
                json.loads(state_path.read_text(encoding="utf-8"))["status"])
            self.assertEqual("", failed_wp.posts[1]["excerpt"]["raw"])

            retry_wp = MockWp()
            retry_glm = MockGlm()
            retry, _, _, _, _ = self.make_flow(
                directory, retry_wp, retry_glm, MockTranslator(retry_wp))
            self.assertEqual("completed", retry.execute(1)["status"])
            self.assertEqual(1, retry_glm.calls)

    def test_retries_only_validation_failures_and_records_success_attempt(self):
        cases = (
            ([VALID_EXCERPT], 1),
            ([rejected_error(1), VALID_EXCERPT], 2),
            ([rejected_error(1), rejected_error(2), VALID_EXCERPT], 3),
        )
        for outcomes, expected_calls in cases:
            with self.subTest(expected_calls=expected_calls), tempfile.TemporaryDirectory() as directory:
                wp = MockWp()
                glm = SequenceGlm(outcomes, before_call=lambda: self.assertEqual([], wp.update_calls))
                translator = MockTranslator(wp)
                flow, _, _, _, _ = self.make_flow(directory, wp, glm, translator)
                state = flow.execute(1)
                self.assertEqual(expected_calls, glm.calls)
                self.assertEqual(expected_calls, state["excerpt_attempts"])
                self.assertEqual(1, len(wp.update_calls)); self.assertEqual(1, translator.calls)
                rejected = list((Path(directory) / "rejected").glob("*.txt")) \
                    if (Path(directory) / "rejected").exists() else []
                self.assertEqual(expected_calls - 1, len(rejected))

    def test_non_excerpt_validation_error_is_not_retried(self):
        with tempfile.TemporaryDirectory() as directory:
            glm = SequenceGlm([SafetyError("not retryable"), VALID_EXCERPT])
            wp = MockWp(); translator = MockTranslator(wp)
            flow, _, _, _, _ = self.make_flow(directory, wp, glm, translator)
            with self.assertRaisesRegex(SafetyError, "not retryable"):
                flow.execute(1)
            self.assertEqual(1, glm.calls); self.assertEqual([], wp.update_calls)
            self.assertEqual(0, translator.calls)


if __name__ == "__main__":
    unittest.main()
