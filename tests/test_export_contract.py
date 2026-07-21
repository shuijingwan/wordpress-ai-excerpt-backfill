from pathlib import Path
import re
import subprocess
import unittest


ROOT = Path(__file__).resolve().parents[1]
PHP_PATH = ROOT / "bin/export-readonly.php"
RUN_PATH = ROOT / "bin/run-readonly-export.sh"
DEPLOY_PATH = ROOT / "bin/deploy-to-production.sh"


class ExportContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.php = PHP_PATH.read_text(encoding="utf-8") if PHP_PATH.exists() else ""
        cls.runner = RUN_PATH.read_text(encoding="utf-8") if RUN_PATH.exists() else ""
        cls.deploy = DEPLOY_PATH.read_text(encoding="utf-8") if DEPLOY_PATH.exists() else ""

    def test_php_exporter_exists(self):
        self.assertTrue(PHP_PATH.is_file())

    def test_runner_exists(self):
        self.assertTrue(RUN_PATH.is_file())

    def test_deploy_script_exists(self):
        self.assertTrue(DEPLOY_PATH.is_file())

    def test_php_contains_all_required_fields(self):
        fields = {
            "schema_version", "export_id", "exported_at", "site_url", "post_id",
            "post_type", "post_status", "title", "slug", "published_at",
            "published_at_gmt", "modified_at", "modified_at_gmt", "permalink",
            "language_source", "language", "language_raw", "categories", "tags",
            "excerpt", "content", "content_sha256",
        }
        for field in fields:
            with self.subTest(field=field):
                self.assertRegex(self.php, rf"['\"]{re.escape(field)}['\"]\s*=>")

    def test_php_requires_polylang(self):
        self.assertIn("function_exists('pll_get_post_language')", self.php)
        self.assertIn("pll_get_post_language($post_id, 'slug')", self.php)
        self.assertIn("pll_get_post_language($post_id, 'locale')", self.php)

    def test_php_suspends_cache_addition(self):
        self.assertIn("wp_suspend_cache_addition(true)", self.php)

    def test_php_uses_resolved_permalink(self):
        self.assertIn("get_permalink($post_id)", self.php)
        self.assertNotRegex(self.php, r"['\"]guid['\"]\s*=>")

    def test_php_hashes_original_content_with_sha256(self):
        self.assertRegex(self.php, r"hash\(['\"]sha256['\"],\s*\(string\)\s*\$row->post_content\)")

    def test_php_query_is_select_with_id_cursor(self):
        self.assertRegex(self.php, r"(?is)SELECT\s+ID,.+ID\s*>\s*%d.+ORDER\s+BY\s+ID\s+ASC.+LIMIT\s+%d")

    def test_php_has_single_explicit_export_limit_constant(self):
        definitions = re.findall(
            r"(?m)^const\s+SWQ_MAX_EXPORT_LIMIT\s*=\s*(\d+)\s*;",
            self.php,
        )
        self.assertEqual(["100"], definitions)

    def test_php_export_limit_validation_precedes_wordpress_access(self):
        validation = self.php.index("readonly_export_bounded_positive_integer(", self.php.index("$maximum_records"))
        wordpress_access = min(
            self.php.index("function_exists('pll_get_post_language')"),
            self.php.index("global $wpdb"),
            self.php.index("while ($exported_count < $maximum_records)"),
        )
        self.assertLess(validation, wordpress_access)

    def test_php_export_limit_is_not_silently_truncated(self):
        self.assertNotRegex(
            self.php,
            r"\bmin\s*\(\s*\$maximum_records\s*,|\bmin\s*\(\s*\$limit\s*,",
        )

    def test_php_and_runner_export_limits_match(self):
        php_limit = re.search(r"(?m)^const\s+SWQ_MAX_EXPORT_LIMIT\s*=\s*(\d+)\s*;", self.php)
        shell_limit = re.search(r"(?m)^MAX_EXPORT_LIMIT=(\d+)\s*$", self.runner)
        self.assertIsNotNone(php_limit)
        self.assertIsNotNone(shell_limit)
        self.assertEqual("100", php_limit.group(1))
        self.assertEqual(php_limit.group(1), shell_limit.group(1))

    def _run_php_exporter_with_limit(self, value):
        php_value = value.replace("\\", "\\\\").replace("'", "\\'")
        wrapper = (
            "$args = array('" + php_value + "'); "
            "include '" + PHP_PATH.as_posix().replace("'", "\\'") + "';"
        )
        return subprocess.run(
            ["php", "-r", wrapper],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )

    def test_php_export_limit_accepts_boundaries(self):
        for value in ("1", "100"):
            with self.subTest(value=value):
                result = self._run_php_exporter_with_limit(value)
                self.assertNotEqual(0, result.returncode)
                self.assertNotIn("must be an integer between 1 and 100", result.stderr)
                self.assertIn("Polylang", result.stderr)

    def test_php_export_limit_rejects_out_of_range_values(self):
        for value in ("0", "-1", "101", "1000"):
            with self.subTest(value=value):
                result = self._run_php_exporter_with_limit(value)
                self.assertNotEqual(0, result.returncode)
                self.assertIn("must be an integer between 1 and 100", result.stderr)
                self.assertNotIn("Polylang", result.stderr)

    def test_php_export_limit_rejects_non_integer_values(self):
        for value in ("abc", "1.5", ""):
            with self.subTest(value=value):
                result = self._run_php_exporter_with_limit(value)
                self.assertNotEqual(0, result.returncode)
                self.assertIn("must be an integer between 1 and 100", result.stderr)
                self.assertNotIn("Polylang", result.stderr)

    def test_php_has_no_forbidden_wordpress_write_calls(self):
        forbidden = [
            "wp_insert_post", "wp_update_post", "wp_delete_post", "update_post_meta",
            "add_post_meta", "delete_post_meta", "update_option", "add_option",
            "delete_option", "wp_set_post_terms", "wp_set_object_terms", "wp_delete_term",
            "wp_insert_term", "wp_update_term", "clean_post_cache", "wp_cache_flush",
            "wp_cache_delete",
        ]
        for function_name in forbidden:
            with self.subTest(function_name=function_name):
                self.assertNotRegex(self.php, rf"\b{function_name}\s*\(")

    def test_php_has_no_database_write_methods_or_sql(self):
        self.assertNotRegex(self.php, r"\$wpdb\s*->\s*(?:insert|update|delete)\s*\(")
        self.assertNotRegex(self.php, r"(?i)\b(?:INSERT|UPDATE|DELETE|REPLACE)\s+(?:INTO|FROM|[A-Za-z_$`])")

    def test_php_has_no_http_or_ai_calls(self):
        for pattern in [r"\bwp_remote_", r"\bcurl_(?:init|exec)\s*\(", r"api\.openai\.com", r"\bglm\b"]:
            with self.subTest(pattern=pattern):
                self.assertNotRegex(self.php.lower(), pattern)

    def test_both_shell_scripts_use_strict_mode(self):
        self.assertIn("set -euo pipefail", self.deploy)
        self.assertIn("set -euo pipefail", self.runner)

    def test_deploy_without_arguments_is_offline_failure(self):
        result = subprocess.run(
            [str(DEPLOY_PATH)], cwd=ROOT, text=True, capture_output=True, check=False,
        )
        self.assertNotEqual(0, result.returncode)
        self.assertIn("Usage:", result.stderr)

    def test_deploy_requires_explicit_deploy_flag(self):
        self.assertIn("--deploy is required for deployment", self.deploy)
        self.assertIn('[[ "${mode}" == "deploy" ]]', self.deploy)

    def test_deploy_dry_run_exits_before_network_commands(self):
        dry_start = self.deploy.index('if [[ "${mode}" == "dry-run" ]]')
        dry_end = self.deploy.index("exit 0", dry_start)
        first_ssh = self.deploy.index('ssh "${SSH_ALIAS}"')
        self.assertLess(dry_end, first_ssh)

    def test_deploy_uses_fixed_tool_directory(self):
        self.assertIn('REMOTE_ROOT="/root/tools/wordpress-ai-excerpt-backfill"', self.deploy)
        self.assertIn("chmod 0700", self.deploy)
        self.assertIn("chmod 0600", self.deploy)

    def test_deploy_does_not_upload_to_wordpress_directory(self):
        upload_lines = [line for line in self.deploy.splitlines() if re.match(r"^\s*scp\b", line)]
        self.assertTrue(upload_lines)
        self.assertFalse(any("/data/wwwroot" in line for line in upload_lines))

    def test_deploy_uploads_temporary_file(self):
        self.assertIn('remote_temporary="${REMOTE_EXPORTER}.new.$$"', self.deploy)
        self.assertRegex(self.deploy, r"(?m)^scp\s+-O\s+--.+remote_temporary")

    def test_deploy_forces_legacy_scp_protocol_on_actual_upload(self):
        upload_commands = [
            line.strip() for line in self.deploy.splitlines()
            if re.match(r"^\s*scp\s", line)
        ]
        self.assertEqual(1, len(upload_commands))
        self.assertRegex(upload_commands[0], r'^scp\s+-O\s+--\s+"\$\{LOCAL_EXPORTER\}"\s+"\$\{SSH_ALIAS\}:\$\{remote_temporary\}"$')

    def test_deploy_lints_before_atomic_move(self):
        lint_position = self.deploy.index(r"\"${REMOTE_PHP}\" -l '${remote_temporary}'")
        move_position = self.deploy.index("mv -f -- '${remote_temporary}' '${REMOTE_EXPORTER}'")
        self.assertLess(lint_position, move_position)

    def test_deploy_uses_executable_fixed_remote_php_for_actual_lint(self):
        self.assertIn('REMOTE_PHP="/usr/local/php/bin/php"', self.deploy)
        self.assertIn(r'[[ -x \"${REMOTE_PHP}\" ]]', self.deploy)
        lint_commands = [
            line.strip() for line in self.deploy.splitlines()
            if re.search(r"\s-l\s+'\$\{remote_temporary\}'", line)
        ]
        self.assertEqual([r"\"${REMOTE_PHP}\" -l '${remote_temporary}' >&2"], lint_commands)
        self.assertNotRegex(self.deploy, r"(?m)^\s*php\s+-l\b")

    def test_deploy_displays_old_hash_and_compares_new_hashes(self):
        self.assertIn("Existing remote SHA-256", self.deploy)
        self.assertIn("local_sha256", self.deploy)
        self.assertIn("remote_sha256", self.deploy)
        self.assertIn('[[ "${local_sha256}" == "${remote_sha256}" ]]', self.deploy)

    def test_deploy_failure_cleans_remote_temporary_file(self):
        self.assertIn("cleanup_remote_temporary", self.deploy)
        self.assertIn("rm -f -- '${remote_temporary}'", self.deploy)

    def test_deploy_never_runs_export(self):
        self.assertNotRegex(self.deploy, r"\bwp\s+eval-file\b")

    def test_runner_uses_fixed_remote_exporter(self):
        self.assertIn('REMOTE_EXPORTER="${REMOTE_ROOT}/bin/export-readonly.php"', self.runner)
        self.assertIn('"$wp_bin" --allow-root eval-file "$exporter"', self.runner)

    def test_runner_uses_fixed_remote_path_wp_and_python(self):
        self.assertIn('REMOTE_PATH="/usr/local/php/bin:/usr/local/bin:/usr/bin:/bin"', self.runner)
        self.assertIn('REMOTE_WP="/usr/local/bin/wp"', self.runner)
        self.assertIn('REMOTE_PYTHON="/usr/bin/python3"', self.runner)
        self.assertIn('export PATH="$remote_path"', self.runner)
        self.assertIn('"$wp_bin" --allow-root eval-file', self.runner)
        self.assertIn('"$python_bin" -c', self.runner)

    def test_runner_checks_fixed_remote_tool_paths_before_export(self):
        export_position = self.runner.index('"$wp_bin" --allow-root eval-file')
        checks = [
            '[[ -x "$wp_bin" ]]',
            '[[ -x "$python_bin" ]]',
            '[[ -x /usr/local/php/bin/php ]]',
        ]
        for check in checks:
            with self.subTest(check=check):
                self.assertIn(check, self.runner)
                self.assertLess(self.runner.index(check), export_position)

    def test_runner_actual_commands_do_not_use_bare_wp_or_python(self):
        self.assertNotRegex(self.runner, r"(?m)^\s*wp\s+eval-file\b")
        self.assertNotRegex(self.runner, r"(?m)^\s*python3\s+-c\b")

    def test_runner_has_one_fixed_allow_root_eval_file_command(self):
        eval_commands = [
            line.strip() for line in self.runner.splitlines()
            if re.match(r'^\s*"\$wp_bin"\s+.*\beval-file\b', line)
        ]
        self.assertEqual(1, len(eval_commands))
        self.assertRegex(eval_commands[0], r'^"\$wp_bin"\s+--allow-root\s+eval-file\s+"\$exporter"(?:\s|$)')

    def test_runner_does_not_send_php_through_stdin(self):
        self.assertNotRegex(self.runner, r"ssh[^\n]*<[^\n]*export-readonly\.php")
        self.assertNotIn("PHP_EXPORTER", self.runner)

    def test_runner_requires_explicit_bounded_limit(self):
        self.assertIn("--limit is required", self.runner)
        self.assertIn("MAX_EXPORT_LIMIT=100", self.runner)
        self.assertRegex(self.runner, r"limit\s*>=\s*1\s*&&\s*limit\s*<=\s*MAX_EXPORT_LIMIT")

    def test_runner_has_no_unlimited_option(self):
        self.assertNotRegex(self.runner, r"--(?:full|unlimited|all)\b")

    def test_runner_checks_remote_file_owner_and_writability(self):
        self.assertIn('[[ -f "$exporter" && ! -L "$exporter" ]]', self.runner)
        self.assertIn('stat -c %%U "$exporter"', self.runner)
        self.assertIn("-perm /022", self.runner)

    def test_runner_uses_remote_temporary_jsonl_then_move(self):
        self.assertIn('temporary_file="$(mktemp "$raw_dir/', self.runner)
        self.assertIn('trap cleanup EXIT', self.runner)
        self.assertIn('mv -- "$temporary_file" "$final_file"', self.runner)

    def test_runner_validates_remote_jsonl_with_python(self):
        self.assertIn('"$python_bin" -c', self.runner)
        self.assertIn("json.loads(line)", self.runner)
        self.assertIn("content_sha256", self.runner)

    def test_runner_does_not_deploy_or_download(self):
        self.assertNotRegex(self.runner, r"(?m)^\s*(?:scp|rsync|sftp)\b")
        self.assertNotRegex(self.runner, r"\bdeploy-to-production\.sh\s+--deploy\b")

    def test_scripts_do_not_print_content_or_excerpt(self):
        terminal_lines = [
            line for line in (self.deploy + "\n" + self.runner).splitlines()
            if re.search(r"\bprintf\b", line)
        ]
        self.assertFalse(any(re.search(r"\b(content|excerpt)\b", line, re.I) for line in terminal_lines))

    def test_scripts_have_no_summary_translation_or_wordpress_write_calls(self):
        combined = self.deploy + "\n" + self.runner
        self.assertNotRegex(combined, r"\b(?:wp_update_post|update_post_meta|wp_insert_post)\s*\(")
        self.assertNotRegex(combined, r"(?i)\b(?:openai|glm|translation-api)\b")


if __name__ == "__main__":
    unittest.main()
