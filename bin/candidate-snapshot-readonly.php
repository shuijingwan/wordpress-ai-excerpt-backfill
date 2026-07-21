<?php
/** Read-only live snapshot for explicit fixed candidates. No content is printed. */

@ini_set('display_errors', '0');

function snapshot_fail($message) {
    fwrite(STDERR, "ERROR: " . $message . PHP_EOL);
    exit(1);
}

if (!isset($args[0]) || !is_string($args[0])) {
    snapshot_fail('base64-encoded candidate JSON is required');
}
$decoded = base64_decode($args[0], true);
$candidates = is_string($decoded) ? json_decode($decoded, true) : null;
if (!is_array($candidates) || count($candidates) !== 42 || array_values($candidates) !== $candidates) {
    snapshot_fail('candidate input must contain exactly 42 rows');
}
if (!function_exists('pll_get_post_language') || !function_exists('pll_get_post_translations')) {
    snapshot_fail('Polylang functions are unavailable');
}

$seen_zh = array();
$seen_en = array();
foreach ($candidates as $candidate) {
    $zh_id = isset($candidate['chinese_post_id']) ? (int) $candidate['chinese_post_id'] : 0;
    $en_id = isset($candidate['english_post_id']) ? (int) $candidate['english_post_id'] : 0;
    if ($zh_id < 1 || $en_id < 1 || isset($seen_zh[$zh_id]) || isset($seen_en[$en_id])) {
        snapshot_fail('candidate IDs must be positive and unique');
    }
    $seen_zh[$zh_id] = true;
    $seen_en[$en_id] = true;
}

foreach ($candidates as $candidate) {
    $zh_id = (int) $candidate['chinese_post_id'];
    $expected_en_id = (int) $candidate['english_post_id'];
    $zh = get_post($zh_id);
    $translations = pll_get_post_translations($zh_id);
    $linked_en_id = isset($translations['en']) ? (int) $translations['en'] : 0;
    $en = $linked_en_id > 0 ? get_post($linked_en_id) : null;
    $content = $zh instanceof WP_Post ? (string) $zh->post_content : '';
    $blocks = $zh instanceof WP_Post ? parse_blocks($content) : array();
    $has_cbp = false;
    $stack = $blocks;
    while ($stack) {
        $block = array_pop($stack);
        if (isset($block['blockName']) && $block['blockName'] === 'kevinbatdorf/code-block-pro') {
            $has_cbp = true;
        }
        if (isset($block['innerBlocks']) && is_array($block['innerBlocks'])) {
            foreach ($block['innerBlocks'] as $inner) {
                $stack[] = $inner;
            }
        }
    }
    $expected_hash = isset($candidate['chinese_content_sha256'])
        ? (string) $candidate['chinese_content_sha256'] : '';
    $record = array(
        'schema_version' => 1,
        'chinese_post_id' => $zh_id,
        'chinese_exists' => $zh instanceof WP_Post,
        'chinese_status' => $zh instanceof WP_Post ? (string) $zh->post_status : null,
        'chinese_language' => $zh instanceof WP_Post ? pll_get_post_language($zh_id, 'slug') : null,
        'chinese_excerpt_empty' => $zh instanceof WP_Post ? trim((string) $zh->post_excerpt) === '' : false,
        'chinese_content_sha256' => $zh instanceof WP_Post ? hash('sha256', $content) : null,
        'is_gutenberg' => $zh instanceof WP_Post ? has_blocks($content) : false,
        'has_code_block_pro' => $has_cbp,
        // Exact phase-one classification was audited locally. An unchanged content hash
        // plus current language/status/structure checks preserves that classification.
        'phase1_eligible' => $zh instanceof WP_Post && hash('sha256', $content) === $expected_hash,
        'linked_english_post_id' => $linked_en_id,
        'english_id_matches_input' => $linked_en_id === $expected_en_id,
        'english_status' => $en instanceof WP_Post ? (string) $en->post_status : null,
        'english_title_sha256' => $en instanceof WP_Post ? hash('sha256', (string) $en->post_title) : null,
        'english_excerpt_sha256' => $en instanceof WP_Post ? hash('sha256', (string) $en->post_excerpt) : null,
        'english_content_sha256' => $en instanceof WP_Post ? hash('sha256', (string) $en->post_content) : null,
    );
    $json = wp_json_encode($record, JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES);
    if (!is_string($json) || fwrite(STDOUT, $json . PHP_EOL) === false) {
        snapshot_fail('failed to emit snapshot for Chinese post ' . $zh_id);
    }
}

fwrite(STDERR, "Read-only candidate snapshot complete: 42 rows; WordPress writes=0; AI API calls=0" . PHP_EOL);
