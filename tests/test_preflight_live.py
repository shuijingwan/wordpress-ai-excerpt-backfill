import contextlib
import csv
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock
import subprocess

from src.candidate_execution import SafetyError
from src.polylang_ssh import PolylangSshChecker
from src.single_candidate_flow import (preflight_live_result, validate_polylang,
                                       special_validated_mixed_structure_eligible)
from src.analyzer import analyze_content
from src.wordpress_clients import WordPressRestClient
from tests.test_single_candidate_flow import CONFIG, CONTENT, MockWp, digest, rows


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "bin/execute-single-candidate.py"
SPEC = importlib.util.spec_from_file_location("execute_single_candidate", SCRIPT)
CLI = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CLI)


class Transport:
    def __init__(self, posts): self.posts = posts; self.calls = []
    def __call__(self, method, url, headers, body, timeout):
        self.calls.append((method, url, headers, body))
        post_id = int(url.split("/posts/")[1].split("?")[0])
        return 200, json.dumps(self.posts[post_id], ensure_ascii=False).encode()


class Polylang:
    def __init__(self, **changes):
        self.calls = []
        self.value = {"chinese_post_id": 1, "chinese_language": "zh",
                      "linked_english_post_id": 1001, "english_post_id": 1001,
                      "english_language": "en", "linked_chinese_post_id": 1}
        self.value.update(changes)
    def check(self, zh_id, en_id):
        self.calls.append((zh_id, en_id)); return dict(self.value)


class PreflightLiveTest(unittest.TestCase):
    @staticmethod
    def resume_evidence(excerpt):
        return (
            {"status": "translation_failed", "chinese_post_id": 1,
             "english_post_id": 1001, "generated_excerpt": excerpt},
            {"sha256": {"chinese_excerpt": digest("")}},
        )

    @staticmethod
    def saved_excerpt():
        return (
            "这是本流程已成功写入、可由执行证据精确验证的中文摘要内容，"
            "恢复时必须严格比对该摘要、中文标题正文与英文目标基线，"
            "从而确保不会把第三方修改误认为可安全继续的执行状态。")

    def write_manifest(self, directory):
        manifest_rows = rows()[:1]
        path = Path(directory) / "manifest.csv"
        with path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=manifest_rows[0].keys())
            writer.writeheader(); writer.writerows(manifest_rows)
        return path

    def test_recovery_restart_and_resume_are_mutually_exclusive(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            CLI.parse_args([
                "--post-id", "1", "--preflight-live", "--resume",
                "--recovery-restart"])

    def test_recovery_restart_missing_evidence_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest = self.write_manifest(directory)
            with self.assertRaisesRegex(SafetyError, "invalid recovery restart evidence"):
                CLI.main([
                    "--post-id", "1", "--preflight-live", "--recovery-restart",
                    "--manifest", str(manifest), "--expected-candidate-count", "1",
                    "--backup-dir", str(Path(directory) / "missing")])

    def test_recovery_restart_preflight_loads_matching_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest = self.write_manifest(directory)
            backup_dir = Path(directory) / "backups"; backup_dir.mkdir()
            excerpt = "上一代已知摘要"
            state = {
                "schema_version": 1, "chinese_post_id": 1,
                "english_post_id": 1001, "status": "prepared",
                "recovery_generation": 2,
                "restart_excerpt_state": "known_previous_generated_excerpt",
                "expected_pre_run_excerpt_sha256": digest(excerpt),
            }
            backup = {
                "schema_version": 1, "chinese_post_id": 1,
                "english_post_id": 1001, "recovery_generation": 2,
                "sha256": {"chinese_excerpt": digest(excerpt)},
            }
            (backup_dir / "chinese-1.execution.json").write_text(
                json.dumps(state), encoding="utf-8")
            (backup_dir / "chinese-1.pre-write.json").write_text(
                json.dumps(backup), encoding="utf-8")
            wp = MockWp(); wp.posts[1]["excerpt"]["raw"] = excerpt
            output = io.StringIO()
            with mock.patch(
                    "src.wordpress_clients.WordPressRestClient", return_value=wp), \
                    mock.patch(
                        "src.polylang_ssh.PolylangSshChecker",
                        return_value=Polylang()), \
                    contextlib.redirect_stdout(output):
                code = CLI.main([
                    "--post-id", "1", "--preflight-live", "--recovery-restart",
                    "--manifest", str(manifest), "--expected-candidate-count", "1",
                    "--backup-dir", str(backup_dir)])
            result = json.loads(output.getvalue())
            self.assertEqual(0, code)
            self.assertTrue(result["preflight_passed"])
            self.assertFalse(result["chinese_excerpt_empty"])
            self.assertTrue(
                result["chinese_excerpt_authorized_by_recovery_restart"])

    def test_cli_accepts_isolated_single_manifest_with_explicit_count(self):
        manifest_rows = rows()[:1]
        with tempfile.TemporaryDirectory() as directory:
            manifest = Path(directory) / "pilot.csv"
            with manifest.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=manifest_rows[0].keys())
                writer.writeheader(); writer.writerows(manifest_rows)
            with mock.patch("src.wordpress_clients.WordPressRestClient", return_value=MockWp()), \
                 mock.patch("src.polylang_ssh.PolylangSshChecker", return_value=Polylang()), \
                 contextlib.redirect_stdout(io.StringIO()):
                code = CLI.main([
                    "--post-id", "1", "--preflight-live", "--manifest", str(manifest),
                    "--expected-candidate-count", "1",
                ])
            self.assertEqual(0, code)

    def test_migration_expected_code_counts_are_checked(self):
        row = rows()[0]
        row["expected_code_block_pro_count"] = "1"
        row["expected_syntaxhighlighter_count"] = "0"
        result = preflight_live_result(row, MockWp(), Polylang(), CONFIG)
        self.assertTrue(result["preflight_passed"], result)
        self.assertEqual(1, result["structure"]["code_block_pro_count"])
        self.assertEqual(0, result["structure"]["syntaxhighlighter_count"])

        row["expected_code_block_pro_count"] = "2"
        result = preflight_live_result(row, MockWp(), Polylang(), CONFIG)
        self.assertFalse(result["preflight_passed"])

    def test_special_validated_mixed_uses_current_structure_not_phase1_history(self):
        row = rows()[0]
        row["expected_code_block_pro_count"] = "1"
        row["expected_syntaxhighlighter_count"] = "0"
        excluded = {
            "phase": "phase-1", "status": "excluded", "eligible": False,
            "exclusion_reasons": ["EXCLUDE_MANUAL_REVIEW"],
        }
        with mock.patch("src.single_candidate_flow.evaluate_phase1_eligibility",
                        return_value=excluded):
            ordinary = preflight_live_result(row, MockWp(), Polylang(), CONFIG)
            special = preflight_live_result(
                row, MockWp(), Polylang(), CONFIG,
                special_validated_mixed=True)
        self.assertFalse(ordinary["preflight_passed"])
        self.assertFalse(ordinary["structure"]["phase1_eligible"])
        self.assertTrue(special["preflight_passed"])
        self.assertFalse(special["structure"]["phase1_eligible"])
        self.assertTrue(special["structure"]["special_mixed_structure_eligible"])
        self.assertTrue(special["structure"]["execution_eligibility"])

    def test_special_validated_mixed_keeps_current_structure_checks(self):
        row = rows()[0]
        row["expected_code_block_pro_count"] = "2"
        result = preflight_live_result(
            row, MockWp(), Polylang(), CONFIG,
            special_validated_mixed=True)
        self.assertFalse(result["preflight_passed"])
        self.assertFalse(result["structure"]["expected_code_block_pro_count_matches"])

    def test_validated_mixed_clean_gutenberg_without_code_can_preflight(self):
        row = rows()[0]
        content = "<!-- wp:paragraph --><p>正文</p><!-- /wp:paragraph -->"
        row.update({
            "chinese_content_sha256": digest(content),
            "expected_code_block_pro_count": "0",
            "expected_syntaxhighlighter_count": "0",
        })
        source = MockWp(); source.posts[1]["content"]["raw"] = content
        excluded = {
            "phase": "phase-1", "status": "excluded", "eligible": False,
            "exclusion_reasons": ["EXCLUDE_NO_CODE_BLOCK_PRO"],
        }
        with mock.patch("src.single_candidate_flow.evaluate_phase1_eligibility",
                        return_value=excluded):
            result = preflight_live_result(
                row, source, Polylang(), CONFIG, special_validated_mixed=True)
        self.assertTrue(result["preflight_passed"])
        self.assertTrue(result["structure"]["execution_eligibility"])

    def test_special_validated_mixed_ignores_shortcode_text_inside_code_block(self):
        row = rows()[0]
        row["expected_code_block_pro_count"] = "1"
        row["expected_syntaxhighlighter_count"] = "0"
        result = preflight_live_result(
            row, MockWp(), Polylang(), CONFIG, special_validated_mixed=True)
        self.assertTrue(result["preflight_passed"])
        self.assertTrue(result["structure"]["special_mixed_structure_checks"][
            "shortcode_structure_clean"])

    def test_cbp_json_attribute_shortcode_text_is_not_current_damage(self):
        content = (
            '<!-- wp:kevinbatdorf/code-block-pro {"code":"\\nArray\\n(\\n'
            '    [code] => 200\\n)"} -->'
            '<div class="wp-block-kevinbatdorf-code-block-pro"><textarea></textarea>'
            '<pre class="shiki"><code></code></pre></div>'
            '<!-- /wp:kevinbatdorf/code-block-pro -->'
        )
        analysis = analyze_content(content, CONFIG)
        self.assertTrue(analysis["shortcodes"]["damaged"])
        self.assertFalse(analysis["shortcodes"]["damaged_outside_protected_ranges"])
        self.assertTrue(special_validated_mixed_structure_eligible(analysis))

    def test_exactly_two_gets_zero_posts_and_no_sensitive_output(self):
        source = MockWp()
        transport = Transport(source.posts)
        wp = WordPressRestClient(cookie="REAL-LOOKING-COOKIE", nonce="REAL-LOOKING-NONCE",
                                 transport=transport)
        polylang = Polylang()
        result = preflight_live_result(rows()[0], wp, polylang, CONFIG)
        self.assertEqual(2, len(transport.calls))
        self.assertEqual(["GET", "GET"], [call[0] for call in transport.calls])
        self.assertTrue(transport.calls[0][1].endswith("/wp-json/wp/v2/posts/1?context=edit"))
        self.assertTrue(transport.calls[1][1].endswith("/wp-json/wp/v2/posts/1001?context=edit"))
        self.assertTrue(all(call[3] is None for call in transport.calls))
        output = json.dumps(result, ensure_ascii=False)
        for forbidden in (CONTENT, "测试标题", "Old content", "REAL-LOOKING-COOKIE", "REAL-LOOKING-NONCE"):
            self.assertNotIn(forbidden, output)
        self.assertEqual([(1, 1001)], polylang.calls)
        self.assertEqual({"wordpress_get": 2, "ssh_readonly": 1, "post": 0,
                          "glm": 0, "translation": 0}, result["request_counts"])
        self.assertTrue(result["preflight_passed"])

    def test_absent_rest_polylang_fields_pass_with_ssh_confirmation(self):
        source = MockWp()
        source.posts[1].pop("lang", None); source.posts[1].pop("translations", None)
        result = preflight_live_result(rows()[0], source, Polylang(), CONFIG)
        self.assertIsNone(result["chinese_language_field"])
        self.assertIsNone(result["polylang_relation_field"])
        self.assertTrue(result["structure"]["phase1_eligible"])
        self.assertTrue(result["preflight_passed"])

    def test_wrong_relation_or_language_fails(self):
        cases = ({"linked_english_post_id": 1999}, {"linked_chinese_post_id": 999},
                 {"chinese_language": "en"}, {"english_language": "zh"})
        for changes in cases:
            with self.subTest(changes=changes):
                result = preflight_live_result(rows()[0], MockWp(), Polylang(**changes), CONFIG)
                self.assertFalse(result["preflight_passed"])
                self.assertFalse(result["structure"]["phase1_eligible"])

    def test_english_identity_and_publish_status_remain_blocking(self):
        source = MockWp(); source.posts[1001]["status"] = "draft"
        self.assertFalse(preflight_live_result(
            rows()[0], source, Polylang(), CONFIG)["preflight_passed"])

        source = MockWp(); source.posts[1001]["id"] = 1999
        self.assertFalse(preflight_live_result(
            rows()[0], source, Polylang(), CONFIG)["preflight_passed"])

    def test_resume_preflight_rejects_english_target_drift(self):
        source = MockWp()
        excerpt = self.saved_excerpt()
        source.posts[1]["excerpt"]["raw"] = excerpt
        source.posts[1001]["title"]["raw"] = "Translated title"
        source.posts[1001]["excerpt"]["raw"] = "Translated excerpt"
        source.posts[1001]["content"]["raw"] = "Translated content"
        result = preflight_live_result(
            rows()[0], source, Polylang(), CONFIG, resume=True,
            resume_evidence=self.resume_evidence(excerpt))
        self.assertFalse(result["preflight_passed"])

    def test_resume_preflight_allows_only_the_evidence_saved_excerpt(self):
        source = MockWp(); excerpt = self.saved_excerpt()
        source.posts[1]["excerpt"]["raw"] = excerpt
        result = preflight_live_result(
            rows()[0], source, Polylang(), CONFIG, resume=True,
            resume_evidence=self.resume_evidence(excerpt))
        self.assertTrue(result["preflight_passed"])

        source.posts[1]["excerpt"]["raw"] = excerpt + "外部修改"
        with self.assertRaisesRegex(SafetyError, "resume evidence"):
            preflight_live_result(
                rows()[0], source, Polylang(), CONFIG, resume=True,
                resume_evidence=self.resume_evidence(excerpt))

    def test_resume_preflight_blocks_chinese_title_or_content_drift(self):
        excerpt = self.saved_excerpt()
        for field, value in (("title", "外部修改标题"),
                             ("content", CONTENT + "外部修改")):
            with self.subTest(field=field):
                source = MockWp(); source.posts[1]["excerpt"]["raw"] = excerpt
                source.posts[1][field]["raw"] = value
                result = preflight_live_result(
                    rows()[0], source, Polylang(), CONFIG, resume=True,
                    resume_evidence=self.resume_evidence(excerpt))
                self.assertFalse(result["preflight_passed"])

    def test_validated_mixed_resume_uses_current_structure_not_phase1_history(self):
        row = rows()[0]; row["expected_code_block_pro_count"] = "1"
        row["expected_syntaxhighlighter_count"] = "0"
        excerpt = self.saved_excerpt()
        source = MockWp(); source.posts[1]["excerpt"]["raw"] = excerpt
        excluded = {
            "phase": "phase-1", "status": "excluded", "eligible": False,
            "exclusion_reasons": ["EXCLUDE_MANUAL_REVIEW"],
        }
        with mock.patch("src.single_candidate_flow.evaluate_phase1_eligibility",
                        return_value=excluded):
            result = preflight_live_result(
                row, source, Polylang(), CONFIG, resume=True,
                resume_evidence=self.resume_evidence(excerpt),
                special_validated_mixed=True)
        self.assertTrue(result["preflight_passed"], result)
        self.assertFalse(result["structure"]["phase1_eligible"])
        self.assertTrue(result["structure"]["execution_eligibility"])

    def test_english_target_drift_is_reported_but_does_not_block_fresh_preflight(self):
        for field in ("title", "excerpt", "content"):
            with self.subTest(field=field):
                source = MockWp()
                source.posts[1001][field]["raw"] = f"changed {field}"
                result = preflight_live_result(rows()[0], source, Polylang(), CONFIG)
                self.assertTrue(result["preflight_passed"])
                audit = result["target_audit"]
                self.assertTrue(audit["target_drift_detected"])
                self.assertTrue(audit["sha256"][f"english_{field}"]["drift"])
                self.assertEqual(
                    rows()[0][f"english_{field}_sha256"],
                    audit["sha256"][f"english_{field}"]["execution_candidate_baseline"])

    def test_all_english_target_drift_is_reported_but_does_not_block_fresh_preflight(self):
        source = MockWp()
        for field in ("title", "excerpt", "content"):
            source.posts[1001][field]["raw"] = f"changed {field}"
        result = preflight_live_result(rows()[0], source, Polylang(), CONFIG)
        self.assertTrue(result["preflight_passed"])
        self.assertTrue(result["target_audit"]["target_drift_detected"])
        self.assertEqual(
            {"english_title", "english_excerpt", "english_content"},
            {field for field, evidence in result["target_audit"]["sha256"].items()
             if evidence["drift"]})

    def test_chinese_source_drift_remains_blocking_for_fresh_preflight(self):
        for field, value in (("title", "changed title"), ("content", CONTENT + "changed"),
                             ("excerpt", "unexpected excerpt")):
            with self.subTest(field=field):
                source = MockWp(); source.posts[1][field]["raw"] = value
                result = preflight_live_result(rows()[0], source, Polylang(), CONFIG)
                self.assertFalse(result["preflight_passed"])

    def test_cli_preflight_does_not_construct_glm_or_translator_or_write_files(self):
        manifest_rows = rows()
        with tempfile.TemporaryDirectory() as directory:
            manifest = Path(directory) / "manifest.csv"
            with manifest.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=manifest_rows[0].keys())
                writer.writeheader(); writer.writerows(manifest_rows)
            wp = MockWp(); output = io.StringIO(); polylang = Polylang()
            with mock.patch("src.wordpress_clients.WordPressRestClient", return_value=wp), \
                 mock.patch("src.polylang_ssh.PolylangSshChecker", return_value=polylang), \
                 mock.patch("src.glm47_excerpt_client.Glm47ExcerptClient") as glm, \
                 mock.patch("src.wordpress_clients.SlyTranslateClient") as translator, \
                 contextlib.redirect_stdout(output):
                code = CLI.main(["--post-id", "1", "--preflight-live", "--manifest", str(manifest),
                                 "--backup-dir", str(Path(directory) / "must-not-exist")])
            self.assertEqual(0, code); glm.assert_not_called(); translator.assert_not_called()
            self.assertEqual([1, 1001], wp.get_calls); self.assertEqual([], wp.update_calls)
            self.assertEqual([(1, 1001)], polylang.calls)
            self.assertFalse((Path(directory) / "must-not-exist").exists())
            rendered = output.getvalue()
            self.assertNotIn(CONTENT, rendered); self.assertNotIn("测试标题", rendered)


class PolylangSshCheckerTest(unittest.TestCase):
    def completed(self, stdout="{}", returncode=0):
        return subprocess.CompletedProcess([], returncode, stdout=stdout, stderr="")

    def valid_json(self, **changes):
        value = {"chinese_post_id": 17374, "chinese_language": "zh",
                 "linked_english_post_id": 17442, "english_post_id": 17442,
                 "english_language": "en", "linked_chinese_post_id": 17374}
        value.update(changes)
        return json.dumps(value)

    def test_runner_uses_fixed_argument_list_stdin_and_shell_false(self):
        runner = mock.Mock(return_value=self.completed(self.valid_json()))
        result = PolylangSshChecker(runner=runner).check(17374, 17442)
        self.assertEqual(17442, result["linked_english_post_id"])
        args, kwargs = runner.call_args
        self.assertIsInstance(args[0], list)
        self.assertEqual("ssh", args[0][0]); self.assertIn("aliyun", args[0])
        self.assertIs(False, kwargs["shell"])
        self.assertIn("$zh_id = 17374;", kwargs["input"])
        self.assertIn("$en_id = 17442;", kwargs["input"])
        self.assertEqual(30, kwargs["timeout"])

    def test_rejects_non_integer_ids(self):
        runner = mock.Mock()
        for value in ("17374", True, 0, "17374; rm -rf /"):
            with self.subTest(value=value), self.assertRaisesRegex(SafetyError, "integers"):
                PolylangSshChecker(runner=runner).check(value, 17442)
        runner.assert_not_called()

    def test_timeout_then_success_retries_once_with_testable_delay(self):
        runner = mock.Mock(side_effect=[
            subprocess.TimeoutExpired(["ssh"], 30),
            self.completed(self.valid_json()),
        ])
        sleeper = mock.Mock()
        result = PolylangSshChecker(runner=runner, sleeper=sleeper).check(17374, 17442)
        self.assertEqual(17442, result["english_post_id"])
        self.assertEqual(2, runner.call_count)
        sleeper.assert_called_once_with(2)
        self.assertEqual([30, 30], [call.kwargs["timeout"] for call in runner.call_args_list])

    def test_oserror_then_success_retries(self):
        runner = mock.Mock(side_effect=[
            OSError("connection reset"),
            self.completed(self.valid_json()),
        ])
        sleeper = mock.Mock()
        PolylangSshChecker(runner=runner, sleeper=sleeper).check(17374, 17442)
        self.assertEqual(2, runner.call_count)
        sleeper.assert_called_once_with(2)

    def test_ssh_255_then_success_retries(self):
        runner = mock.Mock(side_effect=[
            self.completed("", returncode=255),
            self.completed(self.valid_json()),
        ])
        sleeper = mock.Mock()
        PolylangSshChecker(runner=runner, sleeper=sleeper).check(17374, 17442)
        self.assertEqual(2, runner.call_count)
        sleeper.assert_called_once_with(2)

    def test_two_timeouts_report_attempt_count(self):
        runner = mock.Mock(side_effect=[
            subprocess.TimeoutExpired(["ssh"], 30),
            subprocess.TimeoutExpired(["ssh"], 30),
        ])
        sleeper = mock.Mock()
        with self.assertRaisesRegex(
                SafetyError, "read-only Polylang SSH check timed out after 2 attempts"):
            PolylangSshChecker(runner=runner, sleeper=sleeper).check(17374, 17442)
        self.assertEqual(2, runner.call_count)
        sleeper.assert_called_once_with(2)

    def test_invalid_json_and_fields_do_not_retry(self):
        cases = (
            self.completed("notice\n{}"),
            self.completed(json.dumps({"chinese_post_id": 17374})),
        )
        for completed in cases:
            runner = mock.Mock(return_value=completed)
            sleeper = mock.Mock()
            with self.subTest(stdout=completed.stdout), self.assertRaises(SafetyError):
                PolylangSshChecker(runner=runner, sleeper=sleeper).check(17374, 17442)
            runner.assert_called_once()
            sleeper.assert_not_called()

    def test_invalid_response_ids_and_relation_ids_do_not_retry(self):
        cases = (
            self.valid_json(chinese_post_id=999),
            self.valid_json(linked_english_post_id="17442"),
            self.valid_json(linked_chinese_post_id=None),
        )
        for stdout in cases:
            runner = mock.Mock(return_value=self.completed(stdout))
            sleeper = mock.Mock()
            with self.subTest(stdout=stdout), self.assertRaisesRegex(SafetyError, "IDs"):
                PolylangSshChecker(runner=runner, sleeper=sleeper).check(17374, 17442)
            runner.assert_called_once()
            sleeper.assert_not_called()

    def test_polylang_relation_mismatch_is_not_masked_by_retry(self):
        runner = mock.Mock(return_value=self.completed(
            self.valid_json(linked_english_post_id=99999)))
        sleeper = mock.Mock()
        result = PolylangSshChecker(runner=runner, sleeper=sleeper).check(17374, 17442)
        row = {"chinese_post_id": "17374", "english_post_id": "17442"}
        with self.assertRaisesRegex(SafetyError, "linked_english_post_id"):
            validate_polylang(row, result)
        runner.assert_called_once()
        sleeper.assert_not_called()

    def test_non_transport_ssh_failure_does_not_retry(self):
        runner = mock.Mock(return_value=self.completed("", returncode=3))
        sleeper = mock.Mock()
        with self.assertRaisesRegex(SafetyError, "exited with 3"):
            PolylangSshChecker(runner=runner, sleeper=sleeper).check(17374, 17442)
        runner.assert_called_once()
        sleeper.assert_not_called()


if __name__ == "__main__":
    unittest.main()
