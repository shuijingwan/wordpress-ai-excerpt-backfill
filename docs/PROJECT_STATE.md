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
