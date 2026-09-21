# Hierarchical campaign controller

## Scope and evidence

This is an **opt-in, incremental implementation** of the end-to-end architecture.
It replaces reactive per-action selection with persistent goals, committed skill
plans, batched judgments, material accounting, and observation-based verification.
The default `flat` controller and its timed-run retry behavior are preserved.

**This revision does not play Factorio through a rocket launch.** The executable
skill catalog still covers the existing mining bootstrap. The `rocket_launch`
target reaches that verified milestone, then reports an explicit capability
blocker. No fake late-game actions, mock victories, or assumed technologies are
used to make the campaign appear complete.

Offline tests verify controller behavior, not Jev quality or native gameplay.
At PR publication, local validation was `76 passed, 6 skipped`, with no live Jev
or native Factorio run. CI runs the core suite on Python 3.10 and 3.12; optional
live dependencies are not installed there.

### Post-merge native integration, 2026-09-21

The full local dependency environment passes **96 tests**. An existing marked
FLE session was explicitly adopted without resetting its characters or factory.
The native trials were single-writer, with the earlier flat controller stopped
first:

- Initial Jev trial: four requests, no actions. Rounded provider distributions
  and scores exposed overly tight numeric consistency checks.
- Revised Jev trial, pinned to `jev-1.13.0`: four requests, no actions. Numeric
  checks passed, but choice confidence remained below the unchanged 0.45 floor.
  The controller correctly stopped as blocked; this is not a Jev success claim.
- Explicit deterministic-policy trial: two observed, verified actions gathered
  five coal and refueled the existing iron drill. The checkpoint reached the
  bootstrap milestone with the drill working and its output chest connected.
  The chest already contained 633 iron ore from the preceding flat run. This
  trial validates resumed execution and refueling, not hierarchical placement
  from an empty world or an end-to-end campaign.

Local evidence is in `runs/hierarchical-native*.jsonl` and the corresponding
checkpoint/log files (intentionally not committed). The original flat timed
run was then resumed with its original cutoff, not extended by another 12 hours.
Pure-Jev hierarchical operation remains gated by the observed abstention.

## Run the supported milestone

```bash
python -m pip install -e . pytest
python -m jev_factorio --controller hierarchical --backend mock --mock-model \
  --target bootstrap_mining --steps 40 --tick-seconds 0 \
  --checkpoint runs/bootstrap-state.json --log-file runs/bootstrap.jsonl
python -m jev_factorio.evaluation runs/bootstrap.jsonl
```

`--mock-model` overrides configured provider credentials and is restricted to the
mock world. Reusing an existing checkpoint requires explicit resumption; use a
new filename for another fresh mock world. A checkpoint does not restore the
mock or Factorio world itself.

The deterministic comparison uses exactly the same candidate compiler, skill
executor, observations, and completion rules:

```bash
python -m jev_factorio --controller hierarchical --backend mock \
  --policy deterministic --target bootstrap_mining --steps 40 --tick-seconds 0 \
  --log-file runs/deterministic.jsonl
```

Omitting `--target bootstrap_mining` selects the intended `rocket_launch` goal.
Its current expected result is **blocked after bootstrap**, not victory.

## Live adapter precautions

The existing FLE adapter must only be used with its explicitly marked,
dedicated, disposable world. Starting without `--resume` retains the existing
world-initialization/reset behavior; this controller does not remove that guard.
Do not initialize a personal save. Hierarchical live mode requires an explicit
checkpoint and a positive polling interval. Missing model credentials fail
**before backend initialization**; no silent model mock is substituted.

```bash
python -m jev_factorio --backend fle --controller hierarchical \
  --target bootstrap_mining --steps 60 --tick-seconds 2 \
  --checkpoint runs/live-state.json --log-file runs/live.jsonl
```

This command is a live validation procedure, **not a run performed for the PR**.
For a still-running FLE session created with the new session-ID telemetry:

```bash
python -m jev_factorio --backend fle --resume --controller hierarchical \
  --resume-controller --checkpoint runs/live-state.json \
  --target bootstrap_mining --duration-hours 1 --tick-seconds 2 \
  --log-file runs/live.jsonl
```

Old sessions without the session-ID telemetry are rejected by hierarchical mode.
A legacy live session can be explicitly identified without resetting its factory:

```bash
python -m jev_factorio --backend fle --resume --adopt-session \
  --controller hierarchical --target bootstrap_mining --steps 60 --tick-seconds 2 \
  --checkpoint runs/adopted-state.json --log-file runs/adopted.jsonl
```

Stop the previous controller first. Adoption requires a marked world and a valid
live agent character, refuses to overwrite an existing session ID, and requires
a new controller checkpoint. It assigns identity only; it does not reconstruct
old action receipts or claim continuity with an earlier checkpoint. Subsequent
runs use `--resume --resume-controller`, without `--adopt-session`.

A server reload does not preserve FLE's executable runtime; controller memory is
not permission to reset or reinterpret a different world. Pin the provider's
model ID using `--model` when comparing model versions. The logs record requested
and returned model IDs and available usage, separately from backend provenance.
FLE's dedicated peaceful, accelerated-tool bootstrap is not ordinary survival
keyboard/mouse play and must not be reported as such.

## Modules and contracts

| Module | Implemented responsibility |
| --- | --- |
| `planning/goals.py` | Dependency ordering, cycle rejection, verified milestone receipts, explicit unsupported terminal goal. |
| `planning/materials.py` | Batch requirements, inventory and reservation accounting, co-products, explicit recipe alternatives, bounded cycle rejection. |
| `skills.py` | Fully specified plans and steps, current-state preconditions, costs, finite execution bounds, observable postconditions. |
| `judgments.py` | Candidate-specific Choice/Score/Noul questions, request bounds, strict response validation, uncertainty abstention, deterministic ranking. |
| `memory.py` | Atomic versioned checkpoints, session/target binding, pending actions, reservations, bounded outcome history. |
| `controller.py` | Persistent goals and committed plans, revalidation after inference, write-ahead dispatch, outcome verification, bounded recovery. |
| `evaluation.py` | Single-session summaries that distinguish synthetic and tool-assisted evidence; no invented success-rate comparisons. |

### Facts and judgments stay separate

`GameSnapshot` carries backend facts; unknown research, production rates, game
version, and victory telemetry remain `None`. Model assessments never overwrite
those fields. Questions explicitly name the candidate and relevant state fields:
question IDs are routing keys, not assumed instructions visible to Jev.

A decision normally has at most 16 candidates, with one Choice plus three
independent judgments per candidate. The separate observation option keeps every
Choice within 255 options. The configurable 32,000-byte serialized request guard
is a local payload bound, **not a tokenizer estimate or a provider token limit**.
Questions are not free and do not see each other's answers; code combines the
returned judgments. The ranking formula is a heuristic, not a calibrated chance
of success. Low confidence, malformed distributions, contradictory Score values,
or missing evidence cannot authorize a build.

The direct TypeSafe adapter declares a 0.01 answer quantum, based on the rounded
values observed during native integration. Validation checks whether a normalized
distribution and its weighted Score can jointly exist inside those rounding
intervals. This is not arbitrary renormalization: missing labels, out-of-range
values, impossible sums, and contradictory scores are still rejected, and raw
answers remain unchanged in the evidence log. Other clients retain strict
validation unless they explicitly declare the supported quantum. This bounded
rounding treatment supplements the documented
[Score weighted-mean contract](https://docs.typesafe.ai/primitives/score);
it does not weaken confidence or missing-evidence gates.

### Skill execution and recovery

A goal persists while a skill plan executes. The controller does not call Jev
again for every primitive. Preconditions and material availability are checked
again against a fresh observation after inference. A step's reservation and
pending-dispatch entry are atomically saved before actuation.

A successful command string is not success evidence. Movement needs observed
arrival; harvesting needs inventory; placement needs the drill and an output
connection; bootstrap completion needs a working drill, its connected output
chest, and collected ore. The FLE observation adapter counts only the chest at
the chosen drill's drop position, not an unrelated stockpile.

Lost acknowledgments and failed post-action observations leave an in-flight
record. It is observed, not blindly replayed. On a bounded timeout, a mutating
step becomes `uncertain`; a nonmutating production wait can fail and replan.
Late matching evidence can resolve an uncertain action on explicit resumption.
Changed preconditions invalidate a plan; repeated plan failures and repeated
model abstention are bounded. There is no automatic destructive rollback.

Use a **single controller process per world/checkpoint**. Atomic file replacement
prevents torn checkpoints but is not a distributed lock or a transaction across
the game and the filesystem. Do not delete a pending record to retry a build.
Inspect the world and preserve the evidence before reconciling partial work.

### Material calculator limitations

The batch calculator is an independently tested library, not yet connected to a
versioned native recipe export or the bootstrap controller's small skill catalog.
It needs caller-supplied, unlocked recipes and explicit alternative selection.
Its projected batches assume reported raw shortages will first be acquired. It
is not a throughput optimizer, a fluid-network solver, or a spatial planner;
cyclic production raises an error instead of pretending to solve the cycle.

## Remaining work before end-to-end claims

1. Export versioned recipes, technology prerequisites, machine capacities, and
   production history from the actual game; connect them to the planner.
2. Implement and individually validate crafting, smelting, power, transport,
   research, oil/fluid management, spatial construction, and repair skills.
3. Add version-specific native victory telemetry. `victory_source` is an explicit
   contract; no shipped backend fabricates or currently populates rocket success.
4. Evaluate held-out maps with the same tool permissions and resource costs for
   deterministic, direct-choice, and decomposed-Jev policies. Record manual
   interventions, native failures, costs, and game settings. Mock success is not
   a substitute for these runs.
5. Only then test optional forward projections, bounded lookahead, richer
   bottleneck assessment, and later Space Age capability coverage.

## References

- TypeSafe API and exact answer shapes: https://docs.typesafe.ai/api
- Independent question semantics: https://docs.typesafe.ai/primitives
- Current model/request limits: https://docs.typesafe.ai/models
- Existing project backend architecture: [ARCHITECTURE.md](ARCHITECTURE.md)
