import csv
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "bin/build-syntaxhighlighter-preview.py"
SPEC = importlib.util.spec_from_file_location("build_syntaxhighlighter_preview", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class SyntaxHighlighterPreviewTest(unittest.TestCase):
    def test_preview_filters_and_marks_mixed_without_becoming_an_execution_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            raw = root / "raw.jsonl"
            translations = root / "translations.jsonl"
            old = root / "old.csv"
            output = root / "preview.csv"
            syntax = '<!-- wp:syntaxhighlighter/code --><pre>x</pre><!-- /wp:syntaxhighlighter/code -->'
            cbp = ('<!-- wp:kevinbatdorf/code-block-pro --><div '
                   'class="wp-block-kevinbatdorf-code-block-pro"><textarea>x</textarea>'
                   '<pre class="shiki"><code><span class="line">x</span></code></pre></div>'
                   '<!-- /wp:kevinbatdorf/code-block-pro -->')
            records = []
            for post_id, content in ((1, syntax), (2, syntax + cbp), (3, syntax)):
                records.append({
                    "schema_version": 1, "post_id": post_id, "post_type": "post",
                    "post_status": "publish", "language_source": "polylang", "language": "zh",
                    "title": f"标题 {post_id}", "published_at": "2020-01-01 00:00:00",
                    "permalink": f"https://example.invalid/{post_id}/", "excerpt": "",
                    "content": content,
                    "content_sha256": hashlib.sha256(content.encode()).hexdigest(),
                })
            raw.write_text("".join(json.dumps(x) + "\n" for x in records), encoding="utf-8")
            translations.write_text("".join(json.dumps({
                "post_id": post_id, "has_english_translation": True,
                "english_post_id": post_id + 10, "english_post_status": "publish",
            }) + "\n" for post_id in (1, 2, 3)), encoding="utf-8")
            old.write_text("chinese_post_id\n3\n", encoding="utf-8")

            rows = MODULE.build_preview([raw], translations, old, output)
            self.assertEqual([1, 2], [row["chinese_post_id"] for row in rows])
            self.assertEqual(["ready", "mixed"], [row["preview_status"] for row in rows])
            with output.open(encoding="utf-8", newline="") as handle:
                fields = next(csv.DictReader(handle)).keys()
            self.assertNotIn("execution_status", fields)


if __name__ == "__main__":
    unittest.main()
