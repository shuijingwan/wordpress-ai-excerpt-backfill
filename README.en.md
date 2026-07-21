# wordpress-ai-excerpt-backfill

[简体中文](README.md)

`wordpress-ai-excerpt-backfill` is a deterministic audit tool for historical Chinese WordPress posts. Its current purpose is to inventory editor and code formats, identify structural and translation risks, and establish a reviewable eligibility pipeline for a future excerpt backfill workflow.

The project is not currently an excerpt generator or a WordPress write-back tool. Production access in this stage is read-only, explicitly bounded, and independently verifiable.

## Current scope

The audit distinguishes Gutenberg, Classic Editor, and mixed content, together with Code Block Pro, SyntaxHighlighter, Gutenberg core code blocks, classic `pre`/`code`, and known or unknown shortcodes. It also records selected media and structural signals, damaged or unbalanced markup, deterministic risk reasons, and phase-one eligibility.

Phase one is intentionally narrow. A post is eligible only when it is a published Polylang Chinese post with complete Gutenberg and Code Block Pro structures, no SyntaxHighlighter content, no mixed or unknown format, and no manual-review requirement. `gutenberg/plain` and all older formats remain audit or migration candidates rather than automatic excerpt candidates.

Detailed classification rules and output fields are documented in [docs/classification-rules.md](docs/classification-rules.md) and [docs/audit-schema.md](docs/audit-schema.md).

## Project status

Completed:

- Deterministic local detectors, editor/code classification, risk assessment, and phase-one eligibility evaluation.
- A production read-only WordPress exporter with Polylang language filtering.
- A remote runner that requires an explicit export limit and validates JSONL before publishing the remote output file.
- Local JSONL contract validation and privacy-reduced analysis output.
- Protection against shortcode-like text inside SyntaxHighlighter, Code Block Pro, `pre`, and `code` regions.
- Semantic handling of empty structures outside Gutenberg blocks without relaxing detection of real classic content.
- Protection against using the same file as both analyzer input and output, including symbolic and hard links.
- Automated tests for format fixtures, eligibility, export contracts, and local analysis.
- Controlled production export validations with batches of 3, 20, and 100 published Chinese posts. Exported files were downloaded locally and verified with SHA-256 before analysis.

In progress:

- Expanding controlled historical samples.
- Validating historical format boundaries and deterministic risk rules against real data.
- Establishing which low-risk posts can safely enter a future excerpt-generation stage.

Not implemented:

- AI excerpt generation.
- WordPress excerpt write-back.
- Bulk article modification.
- Automatic deployment of any WordPress write tool.
- Translation generation or replacement.

No WordPress post, excerpt, category, tag, database row, or cache has been modified by this project. No AI API has been called.

## Safety boundaries

- The PHP exporter performs read-only WordPress and database access. It does not update posts, metadata, terms, options, or caches.
- The supported export runner requires `--limit N` and permits 1 to 100 exported records per run. This bound is enforced by the runner; the PHP exporter itself requires an explicit finite count but does not independently impose the same hard maximum.
- Deployment never occurs without `--deploy`. `--dry-run` reports the plan without connecting, and running the deployment script without a mode does not deploy.
- Deployment and export are separate commands. Deployment does not start an export, and export does not deploy code.
- The analyzer requires `--expected-count N` in the range 1 to 100.
- Input validation covers the JSONL schema, exact record count, post type, publication status, Polylang language, duplicate post IDs, and content SHA-256.
- Analyzer input and output must resolve to different files, including through symbolic or hard links.
- Formal outputs are written through a temporary file, flushed and synchronized with `flush` and `fsync`, then atomically renamed.
- Production exports and local analysis results are excluded from Git through `.gitignore`.
- There is no database write operation, WordPress update operation, summary generation, translation operation, or AI API integration in the current codebase.

## Production layout

The current production deployment uses SSH alias `aliyun` and keeps the tool outside the web root:

```text
Tool directory:      /root/tools/wordpress-ai-excerpt-backfill
WordPress directory: /data/wwwroot/www.shuijingwanwq.com
Site URL:            https://www.shuijingwanwq.com
```

The exporter is deployed as a fixed local-to-remote artifact under the tool directory. Remote JSONL output is first stored under the tool directory's `data/raw/`. The deployment does not place project files in the WordPress root, plugin directories, theme directories, or another web-accessible location.

## Directory structure

```text
bin/            Command-line entry points for deployment, read-only export, and local analysis
config/         Versioned deterministic classification configuration
docs/           Classification rules and audit schema
src/            Detectors, classifier, risk assessment, analyzer, and eligibility logic
tests/          Artificial fixtures and standard-library automated tests
data/raw/       Raw JSONL downloaded from controlled production exports
data/analysis/  Privacy-reduced local analysis JSONL
```

`data/raw/` and `data/analysis/` contain generated, potentially sensitive local data. Both directories are excluded from Git and must not be committed.

## Verified workflow

1. Run the complete local test suite.
2. Inspect the deployment plan with `--dry-run`.
3. Explicitly authorize deployment of the read-only exporter.
4. Export a bounded batch using `--limit` and, when continuing, `--after-id`.
5. Download the resulting JSONL through a separately controlled operation and verify its SHA-256.
6. Run the local analyzer with the exact expected record count.
7. Review the privacy-reduced analysis output before expanding the sample or considering future automation.

The two production commands remain deliberately separate:

```bash
bin/deploy-to-production.sh --deploy
```

```bash
bin/run-readonly-export.sh --limit 5 --after-id 0
```

Neither command performs excerpt generation or WordPress write-back.

## Local validation

Run all tests without creating project-local bytecode:

```bash
PYTHONDONTWRITEBYTECODE=1 \
python3 -m unittest discover -s tests -v
```

Inspect deployment without connecting:

```bash
bin/deploy-to-production.sh --dry-run
```

Analyze a locally available, validated batch:

```bash
bin/analyze-export.py \
  --expected-count 100 \
  data/raw/example.jsonl \
  data/analysis/example.analysis.jsonl
```

The expected count must match the bounded export. Input and output must be different files.

## Next steps

- Continue expanding read-only samples and validate historical format distributions.
- Review mixed, unknown, damaged, and SyntaxHighlighter content before migration.
- Confirm the low-risk eligibility boundary for future excerpt candidates.
- Design excerpt generation and WordPress write-back as separate later-stage workflows.
- Before any write stage, add explicit backups, dry-run behavior, idempotency, conflict detection, and rollback procedures.

No write-back command should be added or used until those safeguards have been designed, implemented, and independently validated.
