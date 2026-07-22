"""One-call, fixed-ID WordPress and Polylang reader over the production SSH alias."""

import base64
import json
import subprocess

from src.candidate_execution import SafetyError
from src.polylang_ssh import SSH_COMMAND


PHP_TEMPLATE = r'''<?php
@ini_set('display_errors', '0');
$_SERVER['HTTP_HOST'] = 'www.shuijingwanwq.com';
$_SERVER['SERVER_NAME'] = 'www.shuijingwanwq.com';
$_SERVER['REQUEST_METHOD'] = 'GET';
$_SERVER['REQUEST_URI'] = '/';
$_SERVER['HTTPS'] = 'on';
$_SERVER['SERVER_PORT'] = '443';
require "/data/wwwroot/www.shuijingwanwq.com/wp-load.php";
$pairs = json_decode(base64_decode('%s'), true);
if (!is_array($pairs) || count($pairs) < 1 || count($pairs) > 100) {
    file_put_contents('php://stderr', "Invalid fixed batch\n");
    exit(2);
}
if (!function_exists('pll_get_post_language') || !function_exists('pll_get_post_translations')) {
    file_put_contents('php://stderr', "Polylang functions unavailable\n");
    exit(2);
}
$result = array();
foreach ($pairs as $pair) {
    $zh_id = isset($pair[0]) ? (int) $pair[0] : 0;
    $en_id = isset($pair[1]) ? (int) $pair[1] : 0;
    if ($zh_id < 1 || $en_id < 1) {
        file_put_contents('php://stderr', "Invalid fixed post ID\n");
        exit(2);
    }
    $zh = get_post($zh_id);
    $en = get_post($en_id);
    $zh_translations = pll_get_post_translations($zh_id);
    $en_translations = pll_get_post_translations($en_id);
    $post_value = static function ($post) {
        if (!($post instanceof WP_Post)) {
            return null;
        }
        return array(
            'id' => (int) $post->ID,
            'status' => (string) $post->post_status,
            'title' => array('raw' => (string) $post->post_title),
            'excerpt' => array('raw' => (string) $post->post_excerpt),
            'content' => array('raw' => (string) $post->post_content),
        );
    };
    $result[] = array(
        'chinese' => $post_value($zh),
        'english' => $post_value($en),
        'polylang' => array(
            'chinese_post_id' => $zh_id,
            'chinese_language' => pll_get_post_language($zh_id, 'slug'),
            'linked_english_post_id' => is_array($zh_translations) && isset($zh_translations['en'])
                ? (int) $zh_translations['en'] : 0,
            'english_post_id' => $en_id,
            'english_language' => pll_get_post_language($en_id, 'slug'),
            'linked_chinese_post_id' => is_array($en_translations) && isset($en_translations['zh'])
                ? (int) $en_translations['zh'] : 0,
        ),
    );
}
$json = wp_json_encode($result, JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES);
if (!is_string($json)) {
    file_put_contents('php://stderr', "JSON encoding failed\n");
    exit(3);
}
file_put_contents('php://stdout', $json . PHP_EOL);
'''


class BatchReadonlySshSource:
    def __init__(self, posts, relations):
        self.posts = posts
        self.relations = relations

    def get_post(self, post_id):
        return self.posts.get(int(post_id)) or {}

    def check(self, chinese_post_id, english_post_id):
        return self.relations.get((int(chinese_post_id), int(english_post_id))) or {}

    @classmethod
    def fetch(cls, rows, runner=subprocess.run, timeout=120):
        pairs = [[int(row["chinese_post_id"]), int(row["english_post_id"])] for row in rows]
        payload = base64.b64encode(
            json.dumps(pairs, separators=(",", ":")).encode("ascii")
        ).decode("ascii")
        php = PHP_TEMPLATE % payload
        try:
            completed = runner(SSH_COMMAND, input=php, text=True, capture_output=True,
                               timeout=timeout, check=False, shell=False)
        except subprocess.TimeoutExpired as error:
            raise SafetyError("batch read-only SSH query timed out") from error
        except OSError as error:
            raise SafetyError(f"batch read-only SSH query failed: {type(error).__name__}") from error
        if completed.returncode != 0:
            raise SafetyError(f"batch read-only SSH query exited with {completed.returncode}")
        try:
            values = json.loads(completed.stdout)
        except (json.JSONDecodeError, TypeError) as error:
            raise SafetyError("batch read-only SSH response is not valid JSON") from error
        if not isinstance(values, list) or len(values) != len(pairs):
            raise SafetyError("batch read-only SSH response count mismatch")
        posts = {}
        relations = {}
        for pair, value in zip(pairs, values):
            if not isinstance(value, dict) or set(value) != {"chinese", "english", "polylang"}:
                raise SafetyError("batch read-only SSH response fields are invalid")
            zh_id, en_id = pair
            for expected_id, name in ((zh_id, "chinese"), (en_id, "english")):
                post = value[name]
                if post is not None:
                    if not isinstance(post, dict) or post.get("id") != expected_id:
                        raise SafetyError("batch read-only SSH returned an unexpected post ID")
                    posts[expected_id] = post
            relation = value["polylang"]
            if not isinstance(relation, dict):
                raise SafetyError("batch read-only SSH returned an invalid relation")
            relations[(zh_id, en_id)] = relation
        return cls(posts, relations)
