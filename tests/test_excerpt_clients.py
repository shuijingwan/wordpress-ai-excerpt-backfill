import json
import unittest

from src.candidate_execution import SafetyError
from src.excerpt_content import OMISSION, extract_excerpt_source
from src.glm47_excerpt_client import Glm47ExcerptClient
from src.http_json import HttpJsonError, request_json
from src.wordpress_clients import SlyTranslateClient, WordPressRestClient


VALID_EXCERPT = (
    "这篇文章说明一个具体技术问题的背景、排查思路和完整操作过程，并根据实际执行结果总结最终结论，"
    "同时保留关键技术名称，避免加入原文没有提及的效果、判断或营销表达，可直接作为博客文章的中文摘要使用。"
)


class RecordingTransport:
    def __init__(self, response, status=200):
        self.response = response; self.status = status; self.calls = []

    def __call__(self, method, url, headers, body, timeout):
        self.calls.append({"method": method, "url": url, "headers": headers,
                           "payload": None if body is None else json.loads(body), "timeout": timeout})
        return self.status, json.dumps(self.response, ensure_ascii=False).encode()


class ExcerptContentTest(unittest.TestCase):
    def test_removes_cbp_and_protected_html_but_keeps_explanation(self):
        source = (
            '<!-- wp:paragraph --><p>保留正常中文说明。</p><!-- /wp:paragraph -->'
            '<!-- wp:kevinbatdorf/code-block-pro --><div>SECRET_CBP</div>'
            '<!-- /wp:kevinbatdorf/code-block-pro -->'
            '<pre>SECRET_PRE</pre><code>SECRET_CODE</code><textarea>SECRET_TEXTAREA</textarea>'
            '<script>SECRET_SCRIPT</script><style>SECRET_STYLE</style>[caption id="1"]短代码[/caption]'
        )
        result = extract_excerpt_source(source)
        self.assertEqual("保留正常中文说明。 短代码", result)
        for secret in ("SECRET_CBP", "SECRET_PRE", "SECRET_CODE", "SECRET_TEXTAREA"):
            self.assertNotIn(secret, result)

    def test_long_content_uses_head_and_tail(self):
        result = extract_excerpt_source("甲" * 20000 + "中" * 10000 + "乙" * 8000)
        self.assertTrue(result.startswith("甲" * 100))
        self.assertIn(OMISSION, result)
        self.assertTrue(result.endswith("乙" * 8000))
        self.assertNotIn("中" * 100, result)


class GlmClientTest(unittest.TestCase):
    def test_fixed_glm47_payload(self):
        transport = RecordingTransport({"choices": [{"message": {"content": VALID_EXCERPT}}]})
        client = Glm47ExcerptClient(api_key="test-placeholder", transport=transport)
        self.assertEqual(VALID_EXCERPT, client.generate("标题", "正文"))
        call = transport.calls[0]; payload = call["payload"]
        self.assertEqual("glm-4.7", payload["model"])
        self.assertEqual({"type": "disabled"}, payload["thinking"])
        self.assertIs(False, payload["do_sample"]); self.assertIs(False, payload["stream"])
        self.assertEqual(512, payload["max_tokens"])
        self.assertNotIn("temperature", payload); self.assertNotIn("top_p", payload)

    def test_api_key_absent_from_errors(self):
        transport = RecordingTransport({"message": "denied"}, status=401)
        with self.assertRaises(HttpJsonError) as raised:
            Glm47ExcerptClient(api_key="TOP-SECRET-KEY", transport=transport).generate("标题", "正文")
        self.assertNotIn("TOP-SECRET-KEY", str(raised.exception))


class WordPressClientTest(unittest.TestCase):
    def test_excerpt_update_body_has_only_excerpt(self):
        transport = RecordingTransport({"id": 1})
        WordPressRestClient(cookie="cookie-placeholder", nonce="nonce-placeholder",
                            transport=transport).update_excerpt(1, VALID_EXCERPT)
        self.assertEqual({"excerpt": VALID_EXCERPT}, transport.calls[0]["payload"])
        for forbidden in ("title", "content", "status", "slug", "categories", "tags", "meta"):
            self.assertNotIn(forbidden, transport.calls[0]["payload"])

    def test_translation_uses_fixed_body(self):
        response = {"source_post_id": 1, "translated_post_id": 1001, "target_language": "en",
                    "translated_post_type": "post", "post_status": "publish"}
        transport = RecordingTransport(response)
        SlyTranslateClient(cookie="cookie-placeholder", nonce="nonce-placeholder",
                           transport=transport).overwrite(1, 1001)
        self.assertEqual({"input": {"post_id": 1, "source_language": "zh", "target_language": "en",
            "post_status": "publish", "overwrite": True, "translate_title": True,
            "model_slug": "glm-5.2"}}, transport.calls[0]["payload"])

    def test_translation_rejects_wrong_english_id(self):
        response = {"source_post_id": 1, "translated_post_id": 999, "target_language": "en",
                    "translated_post_type": "post", "post_status": "publish"}
        with self.assertRaisesRegex(HttpJsonError, "translated_post_id"):
            SlyTranslateClient(cookie="c", nonce="n", transport=RecordingTransport(response)).overwrite(1, 1001)

    def test_historical_error_response_is_failure(self):
        response = {"code": "swq_full_article_token_validation_failed",
                    "message": "Protected token validation failed", "data": None}
        with self.assertRaisesRegex(HttpJsonError, "swq_full_article_token_validation_failed"):
            SlyTranslateClient(cookie="c", nonce="n", transport=RecordingTransport(response)).overwrite(1, 1001)

    def test_credentials_are_not_in_payload(self):
        transport = RecordingTransport({"id": 1})
        WordPressRestClient(cookie="SECRET-COOKIE", nonce="SECRET-NONCE", transport=transport).get_post(1)
        serialized = json.dumps(transport.calls[0]["payload"])
        self.assertNotIn("SECRET-COOKIE", serialized); self.assertNotIn("SECRET-NONCE", serialized)


class HttpErrorDiagnosticsTest(unittest.TestCase):
    def test_http_500_json_object_is_preserved(self):
        response = {"code": "server_error", "message": "Translation failed", "data": None}
        transport = RecordingTransport(response, status=500)
        with self.assertRaises(HttpJsonError) as raised:
            request_json(transport, "POST", "https://example.invalid", {"Cookie": "cookie"}, {})
        self.assertEqual("HTTP request failed with status 500", str(raised.exception))
        self.assertEqual(response, raised.exception.response)
        self.assertIsNone(raised.exception.response_excerpt)

    def test_http_500_html_is_limited_plain_text_and_redacted(self):
        cookie = "COOKIE-SECRET"; nonce = "NONCE-SECRET"; authorization = "Bearer API-SECRET"
        html = (f"<html><body><h1>Failure</h1><p>{cookie} {nonce} {authorization}</p>"
                + "X" * 2000 + "</body></html>")
        transport = RecordingTransport({}, status=500)
        transport.response = None
        def raw_transport(method, url, headers, body, timeout):
            return 500, html.encode()
        with self.assertRaises(HttpJsonError) as raised:
            request_json(raw_transport, "GET", "https://example.invalid", {
                "Cookie": cookie, "X-WP-Nonce": nonce, "Authorization": authorization})
        error = raised.exception
        self.assertIsNone(error.response); self.assertLessEqual(len(error.response_excerpt), 500)
        self.assertNotIn("<html", error.response_excerpt)
        self.assertNotIn(cookie, error.response_excerpt)
        self.assertNotIn(nonce, error.response_excerpt)
        self.assertNotIn(authorization, error.response_excerpt)


if __name__ == "__main__":
    unittest.main()
