# Production efficiency: first implementation and rollout

## Scope and evidence

This change addresses the furnace-service and short resource-trip patterns in
`review/current-logs-20260921T2110Z/review-logs`. The 100-record projection includes
repeated one-to-three-plate extractions, already-batched ore gathering, and long
native crafting waits. The associated snapshot spans repaired code revisions;
it is not a homogeneous performance benchmark. The new tests are synthetic
regression scenarios, not replayed native game saves.

This PR is the **first implementation**, not the complete automation roadmap.
It does not implement belts/inserters, background crafting, concurrent mutation,
research-supply prefetch, or claim a native performance improvement.

## Changes

### Reach-aware interactions (both scheduling policies)

`FairActions.approach()` checks the actual target with `player.can_reach_entity`
inside its existing RCON command. When already reachable it does not call
`move_to()`. Otherwise the existing collision-free approach, pathfinder, control
lease, and normal walking remain. Actual transfers/configuration still enforce
reach at dispatch. An absent entity falls through to the existing approach used
for construction. No position, reach-distance, or game-speed bonus is introduced.

Counters expose approach requests, skipped approaches, movement requests, mining
batches/starts, and observed mining yields. They are cumulative for the current
adapter instance, reset on reattachment, and are copied into hierarchical JSONL
records as `fair_action_metrics`. They are not wall-time or trajectory evidence.
No extra backend observation or RCON round trip is used for the reach fast path.
The resource-selection/highlight behavior is unchanged; this is not proof that
all visual jitter is fixed.

### Optional ready-work scheduling

`--factory-scheduling serial` remains the default. `ready-work` adds:

- Collection targets of up to ten plates for an actively smelting, sufficiently
  fueled/powered dedicated machine. Targets are capped by remaining demand and
  estimated buffered plus in-flight output. Available small critical demands,
  drained machines, low fuel, and chest outputs are still collected promptly.
- Aggregated raw-material demand for the next requested production batch, with
  the existing maximum of fifty ore/coal/stone per native gather. Wood remains
  one native tree interaction. Mining control is not restarted per ore item.
- Up to eight unique candidate actions from at most thirty-two material-frontier
  probes. Ready sibling ingredients can be gathered, delivered, or crafted while
  another machine smelts. Forecast inventory is never spendable inventory.
- Concrete next-batch descriptions and scheduling context for JEV, without
  changing its confidence floor, request-size budget, or answer validation.
  Deterministic/hybrid fallback preserves compiler priority rather than sorting
  these factory candidates alphabetically. JEV abstention remains visible.
- Yielding of an acknowledged passive `machine_output` wait when a currently
  allowed alternative exists. This records `passive_wait_yielded`, does not mark
  production complete, retains failure history, and replans before dispatch.

All candidates are mutually exclusive alternatives, not an authorized queue.
Before each mutation the existing fresh observation, precondition validation,
inventory reservation, write-ahead checkpoint, and postcondition checks remain.
Prepared/ambiguous actions and pending native handcrafting are not released by
this scheduler. Research waits also remain serial in this first implementation.
No checkpoint schema change or implicit game/supervisor restart is made.

## Activation and validation

Use `--factory-scheduling ready-work` with an explicitly authorized hierarchical
campaign command. Do not change code beneath an active supervisor or restart a
world merely to try this flag. Native comparisons require separately authorized
copies/saves, the same seed and initial conditions, pinned model/configuration,
and code-revision segmentation. Compare serial and ready-work under this same
revision to isolate scheduling; the reach improvement applies to both policies.

Offline commands:

```sh
python -m pip install -e '.[test]'
python -m pytest tests/test_ready_work.py -q
python -m pytest tests/ -q
python -m compileall -q src
```

The minimal `MockBackend` does not simulate the native production catalog;
passing its bootstrap smoke test does not demonstrate furnace throughput. The
new tests use native-planner snapshots and the existing mocked Lua runtime.
Hosted Python 3.10/3.12 checks and native acceptance results must be reported
separately. Do not infer a speedup from action-count reduction alone.

Measure milestone completion in game ticks and wall time, useful plates/science
per interval, manual transfers per delivered unit, approach skip rate, mining
starts per batch, idle intervals, fuel/input starvation, and JEV/fallback shares.
A higher skip count is not a production improvement unless downstream results
also improve. Do not average repaired and unrepaired treatments into one result.

## Next PRs, in order

1. **Native acceptance and scheduler hardening.** Validate the fixed furnace and
   mining scenarios in isolated authorized worlds. Add travel cost, starvation
   urgency, inventory-capacity/forecast refinement, and exact decision-reason
   diagnostics. Calibrate the near-milestone JEV rubric on logged alternatives;
   do not simply lower confidence. Reconcile controller changes with the separate
   research instrumentation PR rather than replacing it.
2. **Acknowledged background jobs.** Distinguish positively acknowledged native
   crafting/production from uncertain dispatches, persist job identities and
   reservations, and test crash/resume plus output consumption before allowing
   independent actions during crafting. Include research resupply planning.
3. **Verified furnace output buffer.** Add inventory-paid, exact-orientation
   inserter/buffer construction with fuel/power and pickup/drop validation.
   Require sustained increasing downstream inventory, not merely entity counts.
4. **Complete automated smelting cell.** Add drill-to-belt-to-furnace input and
   output routes, bounded collision-aware layout, material reservations, and
   sustained-flow verification. Reserve belts needed for committed science
   recipes; carrying belts does not itself authorize spending all of them.
5. **Recurring production and JEV comparisons.** Compare assemblers with repeated
   handcrafting over a bounded demand horizon, then evaluate multiple meaningful
   ready plans using paired baseline/efficient runs and independently verified
   throughput and milestones.

Do not merge or deploy on the strength of mocked tests alone when making native
performance claims. A separate exact-head review and native acceptance gate
remain appropriate before deployment to the supervised campaign.
