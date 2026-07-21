<?php
/**
 * Read-only JSONL export executed by WP-CLI via: wp eval-file -
 *
 * Positional arguments: maximum exported rows, exclusive starting post ID,
 * and scan batch size. All diagnostics go to STDERR.
 */

@ini_set('display_errors', '0');

const SWQ_MAX_EXPORT_LIMIT = 100;

function readonly_export_fail($message, $exit_code = 1) {
    if (function_exists('wp_suspend_cache_addition')) {
        wp_suspend_cache_addition(false);
    }
    fwrite(STDERR, "ERROR: " . $message . PHP_EOL);
    exit($exit_code);
}

function readonly_export_positive_integer($value, $name) {
    if (!is_string($value) || !preg_match('/^[1-9][0-9]*$/', $value)) {
        readonly_export_fail($name . ' must be an explicitly provided positive integer');
    }
    return (int) $value;
}

function readonly_export_bounded_positive_integer($value, $name, $maximum) {
    if (
        !is_string($value)
        || !preg_match('/^[1-9][0-9]*$/', $value)
        || (int) $value > $maximum
    ) {
        readonly_export_fail($name . ' must be an integer between 1 and ' . $maximum);
    }
    return (int) $value;
}

function readonly_export_non_negative_integer($value, $name) {
    if (!is_string($value) || !preg_match('/^(0|[1-9][0-9]*)$/', $value)) {
        readonly_export_fail($name . ' must be a non-negative integer');
    }
    return (int) $value;
}

if (!isset($args[0])) {
    readonly_export_fail('maximum export record count is required; unlimited export is not supported');
}

$maximum_records = readonly_export_bounded_positive_integer(
    (string) $args[0],
    'maximum export record count',
    SWQ_MAX_EXPORT_LIMIT
);
$after_id = isset($args[1])
    ? readonly_export_non_negative_integer((string) $args[1], 'starting post ID')
    : 0;
$batch_size = isset($args[2])
    ? readonly_export_positive_integer((string) $args[2], 'batch size')
    : 100;

if ($batch_size < 1 || $batch_size > 500) {
    readonly_export_fail('batch size must be between 1 and 500');
}
if (!function_exists('pll_get_post_language')) {
    readonly_export_fail('Polylang pll_get_post_language() is unavailable; refusing fallback language detection');
}

if (function_exists('wp_suspend_cache_addition')) {
    wp_suspend_cache_addition(true);
}

global $wpdb;

$export_id = function_exists('wp_generate_uuid4')
    ? wp_generate_uuid4()
    : gmdate('Ymd\THis\Z') . '-' . bin2hex(random_bytes(8));
$exported_at = gmdate('c');
$site_url = home_url('/');
$scanned_count = 0;
$exported_count = 0;
$last_scanned_post_id = $after_id;

fwrite(
    STDERR,
    sprintf(
        "Starting read-only export: limit=%d after_id=%d batch_size=%d%s",
        $maximum_records,
        $after_id,
        $batch_size,
        PHP_EOL
    )
);

while ($exported_count < $maximum_records) {
    $rows = $wpdb->get_results(
        $wpdb->prepare(
            "SELECT ID, post_type, post_status, post_title, post_name, post_date, post_date_gmt, post_modified, post_modified_gmt, post_excerpt, post_content
             FROM {$wpdb->posts}
             WHERE post_type = %s AND post_status = %s AND ID > %d
             ORDER BY ID ASC
             LIMIT %d",
            'post',
            'publish',
            $last_scanned_post_id,
            $batch_size
        )
    );

    if ($wpdb->last_error !== '') {
        readonly_export_fail('read-only post query failed: ' . $wpdb->last_error);
    }
    if (!$rows) {
        break;
    }

    foreach ($rows as $row) {
        $post_id = (int) $row->ID;
        $last_scanned_post_id = $post_id;
        ++$scanned_count;

        $language_slug = pll_get_post_language($post_id, 'slug');
        $language_locale = pll_get_post_language($post_id, 'locale');
        if ($language_slug !== 'zh') {
            continue;
        }

        $category_terms = wp_get_post_terms($post_id, 'category', array('fields' => 'all'));
        if (is_wp_error($category_terms)) {
            readonly_export_fail('category query failed for post ID ' . $post_id . ': ' . $category_terms->get_error_message());
        }
        $tag_terms = wp_get_post_terms($post_id, 'post_tag', array('fields' => 'all'));
        if (is_wp_error($tag_terms)) {
            readonly_export_fail('tag query failed for post ID ' . $post_id . ': ' . $tag_terms->get_error_message());
        }

        $map_term = static function ($term) {
            return array(
                'term_id' => (int) $term->term_id,
                'slug' => (string) $term->slug,
                'name' => (string) $term->name,
            );
        };
        $permalink = get_permalink($post_id);
        if (!is_string($permalink) || $permalink === '') {
            readonly_export_fail('permalink resolution failed for post ID ' . $post_id);
        }

        $record = array(
            'schema_version' => 1,
            'export_id' => $export_id,
            'exported_at' => $exported_at,
            'site_url' => $site_url,
            'post_id' => $post_id,
            'post_type' => (string) $row->post_type,
            'post_status' => (string) $row->post_status,
            'title' => (string) $row->post_title,
            'slug' => (string) $row->post_name,
            'published_at' => (string) $row->post_date,
            'published_at_gmt' => (string) $row->post_date_gmt,
            'modified_at' => (string) $row->post_modified,
            'modified_at_gmt' => (string) $row->post_modified_gmt,
            'permalink' => $permalink,
            'language_source' => 'polylang',
            'language' => 'zh',
            'language_raw' => is_string($language_locale) && $language_locale !== '' ? $language_locale : null,
            'categories' => array_map($map_term, $category_terms),
            'tags' => array_map($map_term, $tag_terms),
            'excerpt' => (string) $row->post_excerpt,
            'content' => (string) $row->post_content,
            'content_sha256' => hash('sha256', (string) $row->post_content),
        );

        $json = wp_json_encode($record, JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES);
        if (!is_string($json)) {
            readonly_export_fail('JSON encoding failed for post ID ' . $post_id . ': ' . json_last_error_msg());
        }
        if (fwrite(STDOUT, $json . PHP_EOL) === false) {
            readonly_export_fail('failed writing JSONL output for post ID ' . $post_id);
        }

        ++$exported_count;
        if ($exported_count >= $maximum_records) {
            break;
        }
    }
}

if (function_exists('wp_suspend_cache_addition')) {
    wp_suspend_cache_addition(false);
}

fwrite(
    STDERR,
    sprintf(
        "Completed read-only export: scanned=%d exported=%d last_scanned_post_id=%d%s",
        $scanned_count,
        $exported_count,
        $last_scanned_post_id,
        PHP_EOL
    )
);
