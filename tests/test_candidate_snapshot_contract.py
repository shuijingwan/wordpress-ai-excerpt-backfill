from pathlib import Path
import re
import unittest


class CandidateSnapshotContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        root = Path(__file__).resolve().parents[1]
        cls.php = (root / "bin/candidate-snapshot-readonly.php").read_text(encoding="utf-8")

    def test_snapshot_has_hard_42_limit_and_polylang_checks(self):
        self.assertIn("count($candidates) !== 42", self.php)
        self.assertIn("pll_get_post_translations", self.php)
        self.assertIn("pll_get_post_language", self.php)

    def test_snapshot_contains_no_wordpress_writes_or_ai_calls(self):
        self.assertNotRegex(self.php, r"\b(?:wp_update_post|wp_insert_post|wp_delete_post|update_post_meta)\s*\(")
        self.assertNotRegex(self.php, r"(?i)\b(?:curl_exec|wp_remote_|openai|glm)\b")

    def test_snapshot_does_not_emit_body_fields(self):
        emitted = re.findall(r"'([^']+)'\s*=>", self.php)
        self.assertNotIn("chinese_content", emitted)
        self.assertNotIn("english_content", emitted)
        self.assertNotIn("english_excerpt", emitted)


if __name__ == "__main__":
    unittest.main()
