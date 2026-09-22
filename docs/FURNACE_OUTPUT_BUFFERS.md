# Furnace-output buffers: inventory-paid construction and observed flow

This optional increment builds on the ready-work and acknowledged-background
branches. It adds one small supported production cell, not the complete belt-fed
factory. No live Factorio session, model request, supervisor restart or deployment
is part of its development or offline validation.

## Activation and scope

Add `--furnace-output-buffers` to an independently authorized **hierarchical FLE**
campaign using `--factory-scheduling ready-work`. It can compose with
`--background-work`; it does not enable that feature implicitly. The flag is off
by default. Mock CLI use and a bootstrap-only target are rejected before backend
initialization because the minimal mock world cannot demonstrate this capability.

New cells are restricted to the existing dedicated stone furnaces for iron,
copper and steel plates. During rocket progression, a furnace must have finished
at least 20 batches, the current request must need at least 10 plates, and at
least 10 units must be available or committed to fueled native production before
construction is proposed. These are initial conservative heuristics, not measured
optimal thresholds. One-off bootstrap needs do not buy infrastructure.

The cell consists of:

```
existing stone furnace -> burner inserter -> wooden output chest
                              ^
                       ordinary coal resupply
```

The burner variant makes the first cell usable without constructing another
power network. It still consumes real fuel. The planner obtains missing items
through existing ordinary recipe/gather plans, commits one construction
prerequisite at a time, and maintains five coal in the inserter when below two.
Existing low-fuel boiler work retains priority. No belts are spent by this change.

## Native layout and paid construction

A fixed local search tests 196 inserter positions/orientations per eligible
furnace (seven by seven cells and four cardinal orientations), bounded to three
supported sources. Native prototype pickup/drop vectors determine endpoints;
buildability is checked for both components. There are no temporary entities,
ghosts, terrain changes, position writes, pickup/drop overrides or inventory grants.

`factory_buffer_build` specifies a source role, exact observed layout identity,
component and receipt. Preparation freezes geometry but does not build. The
existing controller writes its pending checkpoint before execution; ordinary
`FairActions.approach` walks within normal reach. One subsequent native RPC uses
`FairActions.place`, verifies one real item debit, registers the entity and
records the paid receipt. Native source identity and geometry are checked again.

A response timeout is not permission to repeat placement. If the registered
receipt is later observed, the existing pending verifier can reconcile it. If a
native call partially changed the world without a complete receipt, the original
pending record is retained for reconciliation; this implementation never guesses
that rebuilding is safe. An occupied, replaced or rotated component is not adopted.

## Placement is not commissioning

A separate `buffer_flow` verifier accepts a cell only after a finite native
observation window has:

- At least three positive chest-growth samples and at least three received items.
- At least 120 normal game ticks between the first and last sample.
- Continuous sampled conservation of furnace output plus inserter-held output
  plus chest output, allowing only the source's native completed-batch increment.
- The same source and component identities, and the inserter's actual native
  pickup/drop targets connected to that source and chest.

The observer samples at most three cells every 30 game ticks. A wrapper invokes
only the known current fair-control tick handler before sampling; unknown handler
replacement is rejected, and stable captured closures prevent recursive observer
chains on reattachment. Sampling writes telemetry only, never inventory or controls.
The three supported recipes each have one deterministic output per native batch.

The controller and native transfer wrapper prohibit actor insertion into the
output chest, actor extraction from the automated furnace, and collection from an
uncommissioned chest. This avoids ordinary controller actions manufacturing flow
evidence or racing the inserter. Inventory changes that violate conservation,
identity/recipe changes, or changed topology/handlers cause uncertainty, preserving
any independent pending action and reservations.

Commissioning is **finite sampled operational evidence**, not an anti-tamper
proof, continuous throughput guarantee, or causal attribution under arbitrary
human/mod interference. Between-sample compensating manual edits can evade an
inventory-conservation check. Controlled acceptance runs must exclude such input.
After commissioning, ordinary player collection from the chest is allowed; the
original finite proof is retained, while current identity/topology is rechecked.
No new commissioning claim is synthesized from an already full chest.

## Production and background composition

The buffer-aware planner waits for useful chest batches of up to ten plates,
bounded by the immediate requirement and actual/committed supply. Small critical
needs and final partial batches are collected without an arbitrary ten-item
minimum. Empty sources return to normal ore/fuel preparation. Sibling candidates
use the same buffer-aware rules; forecast stock is never spendable inventory.

With acknowledged background crafting enabled, output locks, the one-dispatcher
rule, existing deadlines and uncertainty barriers remain. New construction is not
allowed during a tracked craft. Safe coal maintenance can occur, and ordinary
research-prefetch behavior remains available. Captured native buffer evidence is
included in snapshots and the `buffer_evidence` log projection.

## Persistence and rollout boundaries

There is no additional Python checkpoint schema extension. The parent background
extension is used only when explicitly requested. New action/effect names require
this revision's readers; older code may reject a checkpoint containing them.
Do not switch code/flags under a running controller or roll back a checkpoint
against an advanced world. A persistence failure poisons that controller instance.

Layout ownership, component receipts and flow windows reside in the existing
**ephemeral FLE runtime**. Reattachment to that same runtime preserves them. A
server/runtime reset is not recovered by replaying a Python checkpoint; missing
native evidence blocks actions rather than reconstructing ownership from appearances.
Supervisor, diagnostics and the independent research-event consumers still need
integration review before supervised deployment, especially with background jobs.

Native API references (read, not evidence that the live integration was tested):

- https://lua-api.factorio.com/latest/classes/LuaEntityPrototype.html#inserter_pickup_position
- https://lua-api.factorio.com/latest/classes/LuaEntityPrototype.html#inserter_drop_position
- https://lua-api.factorio.com/latest/classes/LuaEntity.html#pickup_target
- https://lua-api.factorio.com/latest/classes/LuaEntity.html#drop_target

The repository's existing catalog remains restricted to base-game Factorio 2.0.
Acceptance must verify these operations on the pinned runtime, not assume that
current online documentation constitutes a 2.0.77 native execution test.

## Verification commands

```
python -m pip install -e '.[test]'
python -m pytest tests/test_output_buffer_contract.py tests/test_output_buffers_lua.py tests/test_output_buffer_integration.py -q
python -m pytest tests/ -q
python -m compileall -q src
```

The Python contract tests are pure. The Lua tests execute the actual buffer
adapter against a synthetic game fixture, and controller tests use the real
controller with a synthetic backend. They are not game saves, native physics or
performance measurements. Report skips explicitly. Full repository and browser
results belong to the exact hosted PR revision; local partial-workspace results
must not be described as a complete checkout run.

## Next steps

1. Review the exact PR stack, reconcile supervisor/checkpoint/event consumers, and
   perform separately authorized isolated native tests: orientation, reach,
   ingredient debit, fuel usage, sustained output, obstructed layouts, and lost
   responses. Compare serial, ready-work, background work and buffering without
   conflating their effects or pooling code-changing repairs.
2. Add the input half: drill, correctly directed ore belts and furnace loading,
   plus output routing to a buffer or consumer. Validate complete material flow
   and reserve belts/ingredients required by committed science recipes.
3. Extend bounded layout options to electric inserters and multiple furnaces with
   real power and fuel services. Add travel/starvation priorities and inventory
   capacity estimates before attempting arbitrary layouts.
4. Compare recurring assembler production with handcrafting and calibrate JEV
   choices against independently verified milestone time, useful production,
   manual transfers, starvation and uncertainty rates, not action count alone.
