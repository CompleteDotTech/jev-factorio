# Dispatch evidence and progress accounting

This increment changes diagnostic evidence, checkpoint metadata, and offline
reporting. It does **not** change the planner, batch sizes, policy selection,
confidence gates, native Lua commands, actuation postconditions, polling cadence,
campaign cutoff, or supervisor recovery authorization. No native performance
improvement or rocket completion follows from its synthetic tests.

## Attempt identity and persistence

Hierarchical checkpoints and decision records now use schema version **2**.
Each dispatched skill step receives a unique attempt ID saved before execution.
Plan IDs are not attempt IDs: two deliveries to the same lab can use the same
plan ID but must have different attempts and retain their own receipt IDs.
The attempt binds the action, plan and step index, serialized step fingerprint,
starting game tick, existing transfer receipt, and observed entity unit number
when available. Entity identity here is diagnostic, not a replacement for the
native verifier. No command fields or receipt generation rules change.

The original four-field `pending` object is unchanged. New `attempt` metadata
is stored alongside it, preserving the supervisor's comparisons of pending
intent, active plans, steps, reservations, and attempt identity. Repair also
preserves the recorded attempt outcomes. A pending operation must have a
matching attempt in a version-2 checkpoint. Invalid or inconsistent metadata
fails closed; there is no fallback to a new world or an empty checkpoint.

The reader accepts validated version-1 checkpoints and migrates their metadata
in memory. A legacy pending operation gets a deterministic `legacy:` ID derived
from its original session, target, plan, step, and start tick. Poll counts and
acknowledgment changes do not change that ID. Its original start timestamp,
process identity, entity identity, and latency remain unknown. Loading a file
alone never rewrites it; a normally authorized controller save writes version 2.
Unknown schema versions are rejected.

The checkpoint keeps the last 64 finished attempt outcomes. Each JSONL record
carries the latest eight so a completion saved immediately before a failed log
write can appear on a later observation. Evaluation deduplicates those repeated
IDs, and rejects conflicting identities or outcomes. This is a bounded recovery
window, not a complete persistent event database or an exactly-once game protocol.
Diagnostic histories are separate from the history sent to Jev.

## Timing and dispatch stages

Per-iteration phase records distinguish observation, planning, selection,
fresh pre-dispatch observation, dispatch, post-dispatch observation, and
verification. Native insert/extract operations additionally trace the existing
fair role approach and transfer RPC. Optional crafting, output-buffer and
input-route adapters forward diagnostics and trace their applicable preparation,
approach, and RPC stages. `FleBackend.execute_traced` forwards diagnostics
to the existing native execution method. Backends without that optional method
still use their original `execute` contract and receive only outer-stage timing.

For each native transfer substage, the checkpoint records `started` before
entering it and then `returned` or `failed`. A crash can leave `started`; a
returned RPC does not prove the postcondition. Failed observation/verification
evidence is retained separately from dispatch stages. Error codes come from a
fixed vocabulary, never raw exception messages, class names, provider responses,
credentials, or Lua bodies. Existing game state and legacy logs may still contain
private data; this is not a blanket sanitizer for all existing logging.

A phase's `at_utc` is its start time for audit alignment. Its `seconds` is a local
`perf_counter` duration, excluding the initial diagnostic write; null means the
phase did not finish. Outer dispatch time includes nested substage work and
checkpoint overhead, so **do not add nested durations together**. A failed stage
may also include diagnostic overhead. Preparation-to-verification latency uses
an in-memory monotonic start and is null after controller reconstruction or
migration. Persisted UTC timestamps and game ticks are never subtracted to invent
cross-process wall-clock latency. The process ID is a controller-instance UUID,
not an operating-system PID or proof of supervisor ownership.

Extra checkpoint writes have an unmeasured overhead. This increment measures
execution; it does not claim to make it faster. Existing write-ahead and bounded
uncertainty behavior is preserved if a diagnostic write fails.
A failed diagnostic write during exception handling preserves the original
operation exception, including `KeyboardInterrupt` and `SystemExit`.

Background crafting retains a separate durable attempt until observed completion.
Acceptance does not count as verification, and completion does not replace a
concurrent foreground attempt. Passive waits released for ready work finish as
`wait_replanned`. See [background checkpoint compatibility](BACKGROUND_WORK.md)
for legacy jobs with unknown attempt provenance.

## Evaluation

```sh
python -m jev_factorio.evaluation /absolute/captured-campaign.jsonl
```

`verified_actions` now counts unique identified, verified non-wait attempts,
including delayed confirmations recorded as `action: verify`. `verified_waits`
is separate. Satisfied plans skipped before dispatch are not executed attempts.
The count is scoped to IDs present in the supplied log, including carried
checkpoint outcomes; it is not necessarily a full-campaign total.

With any legacy records present, `verified_actions` is null rather than an
invented unique count. `identified_verified_actions` still exposes known IDs;
`legacy_verified_action_records` is explicitly a record count, not a deduplicated
attempt count. A legacy operation migrated to an identified attempt can be counted
on later confirmation, but missing historical dispatches cannot be reconstructed.
Missing timing remains null and is counted separately.

`observed_production_delta` compares captured cumulative production counters at
the log's endpoints, rather than crediting preexisting stock or production. It is
null when counters are unavailable or regress. Counts and timing remain synthetic
for mock worlds. The evaluator is an evidence summarizer, not independent native
attestation, a complete model-billing ledger, or a proof of full-game progress.
A crash before a decision log is written can still leave model usage unknown.

## Offline reconciliation

Use *captured copies*, not a new live backend session:

```sh
python -m jev_factorio.diagnostics /absolute/captured-controller.json \
  --session-id ORIGINAL_SESSION_ID --target rocket_launch \
  --snapshot /absolute/captured-snapshot.json

python -m jev_factorio.diagnostics /absolute/captured-controller.json \
  --session-id ORIGINAL_SESSION_ID --target rocket_launch \
  --log /absolute/captured-campaign.jsonl
```

Without either observation source, the report shows checkpoint evidence only.
The module does not import or initialize backend modules, load `.env`, connect
to RCON or a model, rewrite checkpoints, or invoke actions. It rejects mismatched
sessions and labels observations older than the checkpoint as stale. Its exit
code indicates whether a report was produced, not whether gameplay may resume.

The report distinguishes absent, partial, mismatched, and full matching receipts.
Even a full matching receipt in a capture is not a live authorization. Every
report has `read_only: true` and `replay_authorized: false`. An empty lab, carried
science packs, a missing receipt, or an early-stage exception alone never permits
replaying the old insertion. Preserve pending intent and reconcile with current
authoritative evidence through a separately authorized operational procedure.

## Validation and deployment boundary

Focused tests use real controller/checkpoint/verifier code with explicit fake
worlds and mocked native method boundaries. They cover delayed confirmation,
restart, lost acknowledgment, post-dispatch observation failure, interruption,
partial/missing/mismatched receipts, duplicate log events, checkpoint/log gaps,
phase-write failures, read-only migration, schema rejection, and unchanged mock
bootstrap behavior. No game or live provider is needed:

```sh
PYTHONPATH=src python -m pytest tests/test_attempts.py tests/test_evaluation.py \
  tests/test_diagnostics.py tests/test_planning.py -q
PYTHONPATH=src python -m pytest tests/ -q
python -m compileall -q src
```

A PR is not authorization to replace code under an active controller or supervisor.
Review and deploy at an approved boundary; preserve the original session and cutoff.
An older version-1-only reader **cannot read version-2 checkpoints**. Roll back with
a compatible reader after preserving/reconciling pending work, not by decrementing
a schema number, dropping fields, restoring an older checkpoint against a newer
world, clearing uncertainty, or restarting a world. This increment adds no automatic
rollback, retry, reset, supervisor restart, or live recovery operation.
