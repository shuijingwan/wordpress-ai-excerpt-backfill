"""Small JSON-over-HTTP helper with injectable transport for tests."""

import json
import html
import re
import urllib.error
import urllib.request


class HttpJsonError(RuntimeError):
    def __init__(self, message, response=None, response_excerpt=None):
        super().__init__(message)
        self.response = response
        self.response_excerpt = response_excerpt


def _sensitive_header_values(headers):
    sensitive = {"authorization", "cookie", "x-wp-nonce"}
    return [str(value) for name, value in headers.items()
            if name.lower() in sensitive and value]


def _redact(value, secrets):
    if isinstance(value, dict):
        return {key: _redact(item, secrets) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact(item, secrets) for item in value]
    if isinstance(value, str):
        for secret in sorted(secrets, key=len, reverse=True):
            value = value.replace(secret, "[REDACTED]")
        return value
    return value


def _plain_response_excerpt(text, secrets, limit=500):
    text = re.sub(r"<[^>]*>", " ", text)
    text = re.sub(r"\s+", " ", html.unescape(text)).strip()
    text = _redact(text, secrets)
    return text[:limit] or None


def urllib_transport(method, url, headers, body, timeout):
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.read()
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        raise HttpJsonError(f"network request failed: {type(error).__name__}") from error


def request_json(transport, method, url, headers, payload=None, timeout=60):
    body = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    try:
        status, raw = transport(method, url, headers, body, timeout)
    except HttpJsonError:
        raise
    except Exception as error:
        raise HttpJsonError(f"network request failed: {type(error).__name__}") from error
    if not 200 <= int(status) < 300:
        secrets = _sensitive_header_values(headers)
        try:
            text = raw.decode("utf-8") if isinstance(raw, bytes) else raw
        except UnicodeDecodeError:
            text = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else ""
        try:
            parsed = json.loads(text)
        except (json.JSONDecodeError, TypeError):
            parsed = None
        response = _redact(parsed, secrets) if isinstance(parsed, dict) else None
        excerpt = None if response is not None else _plain_response_excerpt(
            text if isinstance(text, str) else "", secrets)
        raise HttpJsonError(f"HTTP request failed with status {status}",
                            response=response, response_excerpt=excerpt)
    try:
        value = json.loads(raw.decode("utf-8") if isinstance(raw, bytes) else raw)
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError) as error:
        raise HttpJsonError("response is not valid JSON") from error
    if not isinstance(value, dict):
        raise HttpJsonError("JSON response must be an object")
    return value
