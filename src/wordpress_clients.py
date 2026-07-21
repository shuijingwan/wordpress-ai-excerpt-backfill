"""Narrow clients for WordPress post REST and the confirmed overwrite endpoint."""

import os

from src.candidate_execution import SafetyError
from src.http_json import HttpJsonError, request_json, urllib_transport


BASE_URL = "https://admin.shuijingwanwq.com"
TRANSLATE_URL = BASE_URL + "/wp-json/ai-translate/v1/ai-translate/translate-content/run?_locale=user"


class WordPressRestClient:
    def __init__(self, cookie=None, nonce=None, transport=urllib_transport, timeout=60):
        self.cookie = cookie if cookie is not None else os.environ.get("WP_ADMIN_COOKIE")
        self.nonce = nonce if nonce is not None else os.environ.get("WP_REST_NONCE")
        if not self.cookie or not self.nonce:
            raise SafetyError("WP_ADMIN_COOKIE and WP_REST_NONCE are required")
        self.transport = transport; self.timeout = timeout

    def _headers(self):
        return {"Content-Type": "application/json", "X-WP-Nonce": self.nonce, "Cookie": self.cookie}

    def get_post(self, post_id):
        return request_json(self.transport, "GET",
                            f"{BASE_URL}/wp-json/wp/v2/posts/{int(post_id)}?context=edit",
                            self._headers(), timeout=self.timeout)

    def update_excerpt(self, post_id, excerpt):
        return request_json(self.transport, "POST",
                            f"{BASE_URL}/wp-json/wp/v2/posts/{int(post_id)}?context=edit",
                            self._headers(), {"excerpt": excerpt}, self.timeout)


class SlyTranslateClient:
    def __init__(self, cookie=None, nonce=None, transport=urllib_transport, timeout=600):
        self.cookie = cookie if cookie is not None else os.environ.get("WP_ADMIN_COOKIE")
        self.nonce = nonce if nonce is not None else os.environ.get("WP_REST_NONCE")
        if not self.cookie or not self.nonce:
            raise SafetyError("WP_ADMIN_COOKIE and WP_REST_NONCE are required")
        self.transport = transport; self.timeout = timeout

    @staticmethod
    def payload(chinese_post_id):
        return {"input": {
            "post_id": int(chinese_post_id), "source_language": "zh", "target_language": "en",
            "post_status": "publish", "overwrite": True, "translate_title": True,
            "model_slug": "glm-5.2",
        }}

    def overwrite(self, chinese_post_id, expected_english_id):
        response = request_json(
            self.transport, "POST", TRANSLATE_URL,
            {"Content-Type": "application/json", "X-WP-Nonce": self.nonce, "Cookie": self.cookie},
            self.payload(chinese_post_id), self.timeout,
        )
        if response.get("code"):
            raise HttpJsonError(f"translation endpoint error: {response['code']}", response=response)
        expected = {
            "source_post_id": int(chinese_post_id), "translated_post_id": int(expected_english_id),
            "target_language": "en", "translated_post_type": "post", "post_status": "publish",
        }
        for field, value in expected.items():
            if response.get(field) != value:
                raise HttpJsonError(f"translation response mismatch: {field}", response=response)
        return response
