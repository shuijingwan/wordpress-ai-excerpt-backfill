import json
import subprocess
import unittest

from src.batch_readonly_ssh import BatchReadonlySshSource, PHP_TEMPLATE
from src.candidate_execution import SafetyError


class BatchReadonlySshSourceTest(unittest.TestCase):
    def response(self):
        return [{
            "chinese": {"id": 1, "status": "publish", "title": {"raw": "中"},
                        "excerpt": {"raw": ""}, "content": {"raw": "正文"}},
            "english": {"id": 2, "status": "publish", "title": {"raw": "En"},
                        "excerpt": {"raw": ""}, "content": {"raw": "Body"}},
            "polylang": {"chinese_post_id": 1, "chinese_language": "zh",
                          "linked_english_post_id": 2, "english_post_id": 2,
                          "english_language": "en", "linked_chinese_post_id": 1},
        }]

    def test_one_fixed_readonly_ssh_call_returns_posts_and_relation(self):
        runner_calls = []
        def runner(*args, **kwargs):
            runner_calls.append((args, kwargs))
            return subprocess.CompletedProcess([], 0, json.dumps(self.response()), "")
        source = BatchReadonlySshSource.fetch(
            [{"chinese_post_id": "1", "english_post_id": "2"}], runner=runner
        )
        self.assertEqual(1, source.get_post(1)["id"])
        self.assertEqual(2, source.check(1, 2)["linked_english_post_id"])
        self.assertEqual(1, len(runner_calls))
        args, kwargs = runner_calls[0]
        self.assertEqual("ssh", args[0][0]); self.assertIn("aliyun", args[0])
        self.assertIs(False, kwargs["shell"]); self.assertTrue(kwargs["capture_output"])
        self.assertNotIn("正文", kwargs["input"])

    def test_response_count_and_ids_are_strict(self):
        def run(value):
            return lambda *a, **k: subprocess.CompletedProcess([], 0, json.dumps(value), "")
        rows = [{"chinese_post_id": "1", "english_post_id": "2"}]
        with self.assertRaisesRegex(SafetyError, "count mismatch"):
            BatchReadonlySshSource.fetch(rows, runner=run([]))
        value = self.response(); value[0]["english"]["id"] = 3
        with self.assertRaisesRegex(SafetyError, "unexpected post ID"):
            BatchReadonlySshSource.fetch(rows, runner=run(value))

    def test_php_contract_contains_no_wordpress_writes_or_external_apis(self):
        forbidden = ("wp_update_post", "wp_insert_post", "$wpdb->update", "$wpdb->insert",
                     "wp_remote_", "curl_", "SlyTranslate", "GLM")
        for token in forbidden:
            self.assertNotIn(token, PHP_TEMPLATE)
        self.assertIn("get_post($zh_id)", PHP_TEMPLATE)
        self.assertIn("pll_get_post_translations", PHP_TEMPLATE)


if __name__ == "__main__": unittest.main()
