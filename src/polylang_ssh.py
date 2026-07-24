"""Strict read-only Polylang relation check through the fixed production SSH alias."""

import json
import subprocess
import time

from src.candidate_execution import SafetyError


SSH_COMMAND = [
    "ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "aliyun",
    "/usr/local/php/bin/php",
]
EXPECTED_FIELDS = {
    "chinese_post_id", "chinese_language", "linked_english_post_id",
    "english_post_id", "english_language", "linked_chinese_post_id",
}
PHP_TEMPLATE = r'''<?php
@ini_set('display_errors', '0');
$_SERVER['HTTP_HOST'] = 'www.shuijingwanwq.com';
$_SERVER['SERVER_NAME'] = 'www.shuijingwanwq.com';
$_SERVER['REQUEST_METHOD'] = 'GET';
$_SERVER['REQUEST_URI'] = '/';
$_SERVER['HTTPS'] = 'on';
$_SERVER['SERVER_PORT'] = '443';
require "/data/wwwroot/www.shuijingwanwq.com/wp-load.php";
$zh_id = %d;
$en_id = %d;
if (!function_exists('pll_get_post_language') || !function_exists('pll_get_post_translations')) {
    file_put_contents('php://stderr', "Polylang functions unavailable\n");
    exit(2);
}
$translations = pll_get_post_translations($zh_id);
$linked_en_id = is_array($translations) && isset($translations['en']) ? (int) $translations['en'] : 0;
$english_translations = pll_get_post_translations($en_id);
$linked_zh_id = is_array($english_translations) && isset($english_translations['zh'])
    ? (int) $english_translations['zh'] : 0;
$result = array(
    'chinese_post_id' => $zh_id,
    'chinese_language' => pll_get_post_language($zh_id, 'slug'),
    'linked_english_post_id' => $linked_en_id,
    'english_post_id' => $en_id,
    'english_language' => pll_get_post_language($en_id, 'slug'),
    'linked_chinese_post_id' => $linked_zh_id,
);
$json = json_encode($result, JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES);
if (!is_string($json)) {
    file_put_contents('php://stderr', "JSON encoding failed\n");
    exit(3);
}
file_put_contents('php://stdout', $json . PHP_EOL);
'''


class PolylangSshChecker:
    def __init__(self, runner=subprocess.run, timeout=30, max_attempts=2,
                 retry_delay=2, sleeper=time.sleep):
        self.runner = runner
        self.timeout = timeout
        self.max_attempts = max_attempts
        self.retry_delay = retry_delay
        self.sleeper = sleeper

    def check(self, chinese_post_id, english_post_id):
        if (type(chinese_post_id) is not int or type(english_post_id) is not int
                or chinese_post_id < 1 or english_post_id < 1):
            raise SafetyError("Polylang check IDs must be positive manifest integers")
        php = PHP_TEMPLATE % (chinese_post_id, english_post_id)
        for attempt in range(1, self.max_attempts + 1):
            try:
                completed = self.runner(
                    SSH_COMMAND, input=php, text=True, capture_output=True,
                    timeout=self.timeout, check=False, shell=False,
                )
            except subprocess.TimeoutExpired as error:
                if attempt == self.max_attempts:
                    raise SafetyError(
                        f"read-only Polylang SSH check timed out after {attempt} attempts"
                    ) from error
            except OSError as error:
                if attempt == self.max_attempts:
                    raise SafetyError(
                        "read-only Polylang SSH check failed after "
                        f"{attempt} attempts: {type(error).__name__}"
                    ) from error
            else:
                if completed.returncode != 255:
                    break
                if attempt == self.max_attempts:
                    raise SafetyError(
                        "read-only Polylang SSH check exited with 255 after "
                        f"{attempt} attempts"
                    )
            self.sleeper(self.retry_delay)
        if completed.returncode != 0:
            raise SafetyError(f"read-only Polylang SSH check exited with {completed.returncode}")
        output = completed.stdout.strip()
        try:
            value = json.loads(output)
        except (json.JSONDecodeError, TypeError) as error:
            raise SafetyError("read-only Polylang SSH response is not one JSON object") from error
        if not isinstance(value, dict) or set(value) != EXPECTED_FIELDS:
            raise SafetyError("read-only Polylang SSH response fields are invalid")
        if (value["chinese_post_id"] != chinese_post_id
                or value["english_post_id"] != english_post_id
                or type(value["linked_english_post_id"]) is not int
                or type(value["linked_chinese_post_id"]) is not int):
            raise SafetyError("read-only Polylang SSH response IDs are invalid")
        return value
