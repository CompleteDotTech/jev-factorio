# Causal controller instrumentation (research-plan PR 2)

## Scope and activation

Opt-in tracing records the existing flat and hierarchical controller execution;
it does not introduce a new policy, observation, retry, sleep, checkpoint field,
or gameplay verification rule. Existing `--log-file` records, returned dictionaries,
stdout, and schema-1 checkpoints retain their formats.

```sh
python -m jev_factorio --controller hierarchical --backend mock \
  --mock-model --target bootstrap_mining --steps 40 --tick-seconds 0 \
  --checkpoint /tmp/causal-memory.json --log-file /tmp/causal-legacy.jsonl \
  --run-dir /tmp/causal-run
```

Use fresh paths. `--run-dir` is never appended to, resumed, truncated, or repaired.
The directory contains `manifest.json` and `events.jsonl`; neither may alias a
legacy log or checkpoint. CLI validation runs before backend initialization.
Without `--run-dir`, there is no research-file creation, research clock sampling,
trace UUID allocation, or additional backend/model call. Trace IDs use UUIDs,
not the gameplay random-number generator.

The inspected base (`55b68d5fd06d32ec06f2b096a347093f34ec1cb3`) did not contain
the proposed PR-1 logging core. A minimal `research_log.py` sink is therefore
included as a prerequisite. The PR is based on the later fair-controls merge
`47a07010d5ff4ba3401657f23631564cf69b09ad`; its controller files are unchanged
from the inspected base, and all fair-control/backend updates are preserved.
It is **not** the full experiment-provenance design:
Git/environment/world fingerprints, supervisor segments, external hash anchoring,
replay, analytical exports, and experiment aggregation remain outside this PR.
Controllers depend on the small synchronous `EventSink.emit(event_type, payload)`
protocol so a separate logging core can replace this sink without changing policy.

## Event contract

Every disk event has schema `jev-factorio.event.v1`, run ID, contiguous sequence,
UTC, process monotonic time, type, payload, previous hash, and its own hash. The
manifest's canonical SHA-256 anchors the first event. Hashes cover sorted-key,
compact UTF-8 JSON excluding the event's own hash. Payloads are detached copies;
a custom sink cannot mutate the controller's state, requests, or answers.

| Event | Captured evidence |
|---|---|
| `step_started`, `step_finished`, `step_failed` | Controller iteration and uncaught failure, without changing retries or exceptions. |
| `observation` | Existing observation result, phase, full snapshot, or sanitized observation failure. |
| `observation_validated` | Existing hierarchical session, tick, model/backend, and numeric checks succeeded. |
| `candidate_set_created` | Existing compiler output/blocker or flat question batch, in original order. |
| `candidate_set_filtered` | Hierarchical candidates remaining after the existing failure-budget filter. |
| `model_request`, `model_response` | Exact evaluate-boundary state/questions and unvalidated answers, requested/resolved model, usage, or sanitized failure. |
| `decision` | Actual selected plan/action, source, confidence floor, utilities, abstention or fallback after existing interpretation. |
| `plan_committed`, `plan_failed`, `plan_progress` | Existing plan commitment, failure, or advancement over already satisfied steps. |
| `precondition_checked` | Existing fresh-observation precondition result. |
| `checkpoint_written` | Existing checkpoint call; `persisted=false` when no checkpoint path was configured. |
| `action_prepared`, `action_returned` | Write-ahead intent and actual call result/error, parameters, plan/step, and action identity. |
| `verification`, `pending_expired` | Existing predicate result and pending-work budget expiration, not tool success text. |
| `goal_checked`, `goal_activated`, `goal_completed` | Existing goal predicate calls and actual milestone transitions. |
| `run_started`, `run_finished` | Sink lifecycle; finishing a trace is not winning the game. |

`observation` records are initially labelled `not_yet_validated`. An invalid
session/tick observation is evidence of a failed read/validation, not permission
to act. Flat control has no comparable validation or postcondition verifier;
its `verification.verified` is explicitly `null`. No new flat milestone predicate
or game read is introduced to fill that gap.

Model requests are captured where `evaluate` is actually called, **after**
question budgeting. Generated, failure-filtered, and actually offered candidates
can therefore be distinguished. A budget rejection emits no model call. Raw
answers are captured before answer validation; malformed/nonfinite values do
not become valid authorizations. Python tuples become JSON arrays, matching
request serialization. Unknown or redacted values are explicitly represented.
Mock calls retain `is_mock=true`; neither their timings nor milestones are native
Factorio or live-provider benchmarks.

## Correlation and restart limits

Each trace has a unique `trace_id` plus observation, decision-cycle, model-call,
and action IDs. `decision_id` names a controller iteration, **not** a claim that
a model was queried. Committed-plan steps and pending polls do not create
phantom model decisions. `plan_id` remains the existing plan ID; separate action
IDs distinguish actual dispatches of the same plan step.

Pending polls link to the original action within one trace. The mock backend's
already-existing idle calls are recorded separately as `role=mock_clock_advance`,
with `related_action_id` pointing to the pending action. They are not retries
of the pending mutation.

Checkpoint schema 1 is unchanged. After reconstruction, the old action ID is not
invented: verification uses `action_id=null`, `action_origin=checkpoint_or_external`
and the captured plan, step index, and started tick. Use a **new** run directory
when restarting. This trace alone does not prove globally unique attempts across
processes, authorize replay, or replace native transfer receipts. The separate
attempt-evidence PR #3 changes checkpoint schema and overlaps controller code;
reconcile that change explicitly rather than merging one implementation over it.

## Timing and behavior equivalence

`duration_ns` uses `perf_counter_ns()` immediately before and after the existing
operation. Payload capture, redaction, serialization, and event fsync are outside
that operation's measured duration. This covers observations, candidate compilation,
model evaluation (including client HTTP/decoding), actuation, existing predicate
checks, plan advancement, and existing checkpoint writes. Error paths also carry
durations. There is no extra model evaluation or backend observation for timing.

This is **logical/call-sequence equivalence**, not zero-overhead instrumentation.
Synchronous persistence adds unmeasured wall time and can change an independently
advancing live world's tick at the next existing observation or how many decisions
fit an existing deadline. No native speedup, unchanged real-time trajectory, or
end-to-end performance result is claimed. The run loop, sleep values, request
budgets, precondition rules, confidence gates, actuation parameters, postconditions,
and timeout/poll budgets are unchanged.

## Durability and failure semantics

The existing hierarchical order becomes:

```text
existing pending assignment
existing durable checkpoint
checkpoint_written event
fsync(action_prepared)
existing act()/execute() exactly once
action_returned event
existing returned-state checkpoint
existing observation
existing predicate -> verification event
existing state/goal updates and checkpoint
```

Every built-in sink event is flushed and fsynced before `emit` returns. On POSIX,
new directory entries are fsynced at initialization, including newly created
parents. Python offers no portable Windows directory-fsync equivalent; the manifest
reports `directory_fsync=false` there. Actual persistence still depends on the
filesystem/device honoring fsync. No cross-process/shared-writer use is supported.

A preparation logging failure prevents the next mutation. A logging failure after
dispatch preserves the original pending checkpoint, and is not misclassified as
a provider outage or ambiguous backend exception. The trace is poisoned so later
steps cannot silently continue unlogged. If logging a primary exception also fails,
the primary exception is preserved, but the poisoned trace still blocks later work.
This deliberate opt-in fail-stop behavior is the exception to behavior equivalence
under recorder failure. There is no automatic replay, checkpoint reset, truncation,
repair, or logging-error retry.

`action_prepared` proves recorded **intent**, not that Factorio received a command;
`action_returned` proves a call returned, not its postcondition. A failed observation
leaves pending work for the original reconciliation path. Flat control gains durable
intent but does not acquire a recovery checkpoint or replay guarantee.

```sh
python -c 'from jev_factorio.research_log import verify_run; print(verify_run("/tmp/causal-run"))'
```

This verifier is read-only. Chaining detects modified/reordered/missing interior
records and partial trailing lines. An entirely removed suffix or a rewritten
whole chain cannot be authenticated without an independently trusted head. A
`clean_finish` footer means trace closure only; inspect its outcome and controller
status separately.

## Secrets and publication

No raw argv, environment dump, headers, URLs, client object, or exception body is
captured. Errors use a fixed category vocabulary and optional bounded HTTP status.
Known client/CLI credentials, credential-named fields, and Bearer strings are
redacted. Nonfinite numbers use an `invalid_numeric` tag. Opaque values are labelled,
never stringified. Failure events do not inherit prior model usage or resolved IDs.

Snapshots, questions, model answers, and backend results are still data-bearing
artifacts. Unknown credentials in arbitrary free text cannot be guaranteed absent;
review files before publishing them. Existing legacy log contents are intentionally
unchanged, so these additional research-log redaction rules do not retrofit old logs.

## Offline validation

`tests/test_causal_trace.py` compares logging off/on for flat control and hierarchical
`jev`, `deterministic`, and `hybrid` policies under normal, abstaining, malformed,
and timeout responses. Comparisons include exact checkpoint-write bytes, legacy JSONL,
stdout, returned records, ordered backend calls, model requests, and final snapshots.
Tests also cover durable pre-dispatch intent, recorder/checkpoint faults, lost
acknowledgments and observations, resume without replay, false success text,
precondition changes, permanent/transient HTTP failures, malformed answers, bounded
candidate requests, factory execute parameters, timing isolation, interrupt handling,
redaction, and aliased output paths.

An additional implementation-time subprocess comparison against the untouched
pinned base source matched all 16 scenarios with instrumentation both disabled and
enabled. This is stronger than only comparing the two modes of the modified code,
but remains synthetic validation. No live Factorio/provider calls, world reset,
active-campaign changes, deployment, or supervisor intervention were performed.

The local workspace was assembled from complete, Git-blob-verified source copies;
it was not a full checkout. Report local selected tests separately from the full
repository Python 3.10/3.12 CI suite. Passing offline tests does not establish native
rocket completion, provider latency, or durability under every power-loss scenario.
