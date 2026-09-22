# Hierarchical campaign controller

## Scope and evidence

This is an **opt-in, incremental implementation** of the end-to-end architecture.
It replaces reactive per-action selection with persistent goals, committed skill
plans, batched judgments, material accounting, and observation-based verification.
The default `flat` controller and its timed-run retry behavior are preserved.

**A complete native rocket-launch playthrough remains unverified.** The FLE
controller now includes native production scheduling through rocket construction
and launch, rather than stopping at the bootstrap capability boundary. Synthetic
tests and source coverage must not be reported as a successful native campaign.

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

### Native production increment, 2026-09-21

The expanded local suite passes **128 tests**. A resumed native Factorio 2.0.77
session completed `iron_smelting`: it gathered stone, hand-crafted and placed a
stone furnace, delivered coal and ore, and collected ten natively produced iron
plates. The original factory and session identity were preserved. This was a
deterministic-policy trial, not evidence of JEV campaign performance.

An earlier trial exposed an offline character-binding failure. The agent stopped
with an ambiguous pending action; reconnecting the viewer restored the existing
character with its 50 ore and five coal intact. Runtime references were reconciled
without creating replacement items or resetting the world, then the original
pending binding was verified rather than replayed. Offline binding now fails its
preconditions and has a regression test. This manual recovery is not a supported
automatic reconnect mechanism.

The subsequent `steam_power` trial also completed natively at tick 506015.
It crafted and placed the lab, offshore pump, boiler, and steam engine, built
water and steam pipes, and connected power poles. The lab had positive energy
and shared electric network ID 1 with the engine. Electronics and steam-power
crafting/production research triggers completed without unlocking technologies
through a script. Output-only fluid boxes required inspecting their actual pipe
neighbors to identify the connected fluid segment; the pending water connection
was reconciled without building duplicate pipes.

Evidence remains local in `runs/native-smelting-v4-state.json`,
`runs/native-steam-state.json`, and their JSONL streams. Lab-driven research, oil
processing, and rocket completion still require separate live acceptance; tests
alone do not establish those milestones. The hybrid rocket campaign resumes the
original timed window, ending at 16:01:15 UTC rather than starting another 12 hours.

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
The mock backend still reports **blocked after bootstrap**, not victory. Only
the FLE backend supplies the native catalog and factory execution primitives.

## Native production scheduling

The `iron_smelting`, `steam_power`, `automation_science`, and `rocket_launch`
targets use a bounded recursive compiler over the running Factorio 2.0 base-game
recipe and technology catalog. It schedules gathering, direct native hand-crafting,
dedicated machine placement, conserved inventory transfers, physical pipes and
power poles, science delivery, native research, and rocket construction.

The agent carries solids; this is not a belt-automated factory. Fluid recipes
select explicit alternatives, with tanks for oil co-products. Research includes
2.0 craft-item and crude-oil mining triggers. Connection verification reads native
fluid segment or electric network identities, not the connector's success text.
Transfer receipts bind to entity unit numbers and actual inserted quantities.
Machine batches and fuel top-ups respect native item stack sizes. Fluid branches
reuse pipes on the observed matching fluid segment; multi-output sources select
an explicit outlet rather than assuming the nearest outlet carries the right fluid.
Launch completion requires an increase in the native force launch count.

Native hand-crafting requires a connected game client bound to the existing agent
character. Offline binding is rejected before dispatch. Viewer reconnection and
server reload are not automatic recovery paths: preserve the checkpoint and world
and reconcile the character and pending action before another writer starts.
The native catalog, runtime state, and local run logs are not committed fixtures.

Use `--policy hybrid` to retain audited JEV judgments while allowing deterministic
compiled fallback after abstention. The default `jev` policy still blocks on
abstention; fallback is never presented as a successful JEV choice.

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
| `planning/goals.py` | Dependency ordering, cycle rejection, native milestone and rocket completion checks. |
| `planning/catalog.py`, `planning/factory.py` | Version-bound native facts and bounded production/research scheduling. |
| `factory_contract.py`, `backends/native_factory.py`, `lua/` | Bounded commands, native transfers, topology and progress verification. |
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
## Partial native gather recovery

An unacknowledged gather is not replayed. Reconciliation requires a measured
inventory increase above its committed starting quantity, still below its target.
The controller retains a typed `gather_partial_progress` history receipt before
clearing that plan. A matching session, site, target, observation and strictly
smaller remainder can then receive a stable `:remainder-quantity:N` plan identity.
Original retry counters are preserved; unchanged commands and exhausted remainder
identities remain blocked. Missing or mismatched evidence fails closed. The same
candidate compilation rule applies to serial and ready-work scheduling.
