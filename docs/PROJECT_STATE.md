# Project state

## Source-restart excerpt recovery

Source-restart generations previously had an inconsistent baseline contract:
the history-migration coordinator recognized only an empty excerpt or an old
`execution.generated_excerpt`, while the single-candidate executor required the
generation's SHA-bound `expected_pre_run_excerpt_sha256`. After an
`excerpt_generation_failed` transient failure before any new excerpt write,
these checks could disagree.

The coordinator now uses the same SHA-bound restart excerpt baseline contract
as the executor. Both `empty` and `known_previous_generated_excerpt` states are
accepted only when the generation, post IDs, pre-write excerpt SHA, expected
SHA, and current production excerpt all match; empty/non-empty semantics remain
explicit and fail closed. An `excerpt_generation_failed` execution may be
re-entered only with that exact prepared-generation evidence.

Real production validation completed successfully for both transient network
samples:

- zh=1149: source-restart generation excerpt `TimeoutError`; recovered through
  `retry_excerpt_generation` to `completed`.
- zh=1193: source-restart generation excerpt `RemoteDisconnected`; source was
  unchanged and the article recovered through `retry_excerpt_generation` to
  `completed`.

No deployment or production write is implied by this state note.

## Preflight transient-network recovery

The recovery state machine previously had no safe entry point for a transient
network failure during executor preflight when no execution or pre-write
artifact had yet been created. Such failures could leave coordination at
`ready_for_execution`, or make recovery require evidence that correctly did
not exist. Preflight transient failures are now explicitly persisted at the
preflight boundary, and the narrow `preflight_transient_retry` recovery path
revalidates the fixed manifest, validation evidence, production Chinese and
English baselines, empty Chinese excerpt, structure, eligibility, and
Polylang relation before authorizing a fresh run. The path remains fail-closed
for source drift, English drift, mutations, non-transient failures, or any
artifact.

The executor artifact scan now requires an exact post-ID filename boundary,
so evidence for posts such as `7692` cannot be mistaken for post `769` (and
similarly for `7624`, `7512`, and other prefix collisions).

Real production validation for batch `mixed-syntaxhighlighter-20260817-01`
completed all 20 of 20 articles:

- zh=800: SHA-bound `excerpt_generation_failed` evidence was reauthorized;
  a subsequent preflight transient failure safely returned to
  `retry_excerpt_generation`, then completed.
- zh=769 and zh=762: first preflight SSL EOF failures had zero execution and
  pre-write artifacts; `preflight_transient_retry` safely authorized fresh
  execution, then completed.
- zh=751: first preflight SSL EOF entered `preflight_transient_retry`, then a
  separate `paragraph_run_punctuation_only` content issue was manually fixed;
  `restart_from_current` revalidated the source and the article completed.

No WordPress field was manually changed for the recovery-state-machine fix;
no deployment is implied by this state note.
