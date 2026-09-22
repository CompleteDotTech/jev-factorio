# Furnace input belts: bounded owned routes

## Scope

This increment is stacked on the output-buffer implementation in PR #31 at
`599a9326052da1e7577ad0546831aa75ca6613e2`, itself on #29 and #28. It adds:

```
observed iron/copper patch -> burner drill -> directed transport belts
  -> burner loading inserter -> existing stone furnace
  -> previously commissioned output inserter -> output chest
```

The route moves ore and finished products without player carrying along that
chain. Coal resupply and eventual chest collection remain player actions. This
is not a fully self-sustaining fuel network, a general factory-layout solver,
steel input automation, or a demonstrated native-game speedup.

The first implementation supports only new, exclusively owned yellow-belt
routes into the dedicated iron/copper stone furnaces. The output buffer must
already be commissioned. Existing belts are not reused, joined, side-loaded,
removed, rotated, or silently adopted. Electric drills/inserters, underground
belts, splitters, multiple furnaces, automatic depletion rerouting, and mining
productivity bonuses are outside this increment.

## Activation

Add these options to an independently authorized hierarchical FLE campaign:

```
--factory-scheduling ready-work --furnace-output-buffers --furnace-input-belts
```

`--background-work` is a separate explicit option. It is not implicitly enabled.
The input flag requires the output flag; existing output validation also requires
hierarchical FLE and a native production target. Invalid combinations are rejected
before backend initialization. All defaults remain unchanged.

Do not replace running controller/supervisor code, reset a world, or restore an
older checkpoint against a newer world to try these options. PR creation is not
automatic approval for merge or live deployment.

## Layout, construction, and reserves

Native survey examines at most 128 observed resources within 40 tiles, probes at
most eight candidate drill centers, four cardinal directions, and a bounded
local furnace-loading search. A route is at most 64 belt tiles. A shared cache
limits unique belt-buildability probes to 4,096 per survey. Failed surveys are
throttled to 300 game ticks. Cached offers retain their layout ID until invalid.
No ghosts, temporary entities, terraforming, or geometry writes are used as probes.

Native prototype vectors determine the drill output and inserter pickup/drop
cells. Layouts obey the normal two-tile drill and one-tile component grids. After
construction, native mining-area and pickup/drop/belt-neighbour relationships
must independently agree; predicted geometry is not proof of connection.

Construction runs downstream first: loading inserter, last belt back through
first belt, and drill last. The drill and loader cannot receive fuel through the
adapter until all components exist and topology is verified. The planner acquires
the complete remaining kit and a coal buffer before selecting a build. Each paid
component is still one bounded command with its own write-ahead pending state.

The kit preserves enough transport belts for a twenty-pack logistic-science
batch, calculated from the native recipe. That floor is fixed when the route
is committed. An unsupported reserve above 200 fails rather than being truncated.
The remaining kit plus floor is rechecked before walking and before each build.
A build debits only its own item; the remaining reservation is checked again
before subsequent placement. This is not a global materials optimizer.

Every build uses the existing normal-speed, normal-reach, original-actor
`fair.place` path. The atomic build RPC records the actual native entity and paid
receipt after placement. A failed or lost response remains ambiguous, even if
some metadata or a component might have been created. No component is blindly
replayed, repaired by item grants, or removed to make the layout fit.

## Separate construction and material-flow evidence

`input_component` verifies the exact layout, source, part, paid receipt and
observed entity. `input_flow` separately commissions the entire production route.
It requires stable owned identities and native connectivity, plus at least:

- 120 game ticks and three positive output-chest growth samples;
- three mined ore, three delivered ore, and three newly attributable plates;
- sampled conservation through both the ore and output sides.

Ore conservation compares depletion in the actual drill mining area with changes
in belt inventories, loader-held ore, furnace buffered/in-flight ore, and native
finished batches. Output conservation compares those finished batches with
furnace results, output-arm-held plates, and chest growth. Initial furnace input,
in-flight work, and uncollected output are subtracted from the completion test:
preloaded material cannot by itself establish drill-fed output.

The 1:1 iron/copper accounting intentionally rejects nonzero mining productivity,
including changes after layout selection. Mixed-resource patches, foreign belt
contents, unexpected mining targets, counter regressions, broken topology, or
missing components stop progress as uncertain rather than producing a success.
The controller does not manually feed ore into a complete route, drain its output
before commissioning, or manually gather that same ore while the route owns its
sampled supply. Fuel transfers remain ordinary inventory-paid actions.

Sampling occurs within the existing native observation call. It adds no RCON
polls or tick handler, and does not touch engine production speed or inventories.
Additional survey/sampling CPU cost remains unmeasured. On-tick fair controls and
the output-buffer sampler retain their existing ownership. Observer/transfer
reattachment captures stable prior closures and rejects an unexpected owner.

Commissioning is finite, sampled operational evidence. It is not continuous
throughput assurance, proof against arbitrary compensating manual edits between
samples, or authentication of the entire world. Geometry/topology and ownership
remain checked after commissioning; conservation proof is not continually renewed.
A later depleted or starved route may hit the existing bounded wait/failure gate;
this implementation does not relocate the drill or silently revert to manual ore.

## Checkpoints and model state

The opt-in memory class adds `input_routes_schema: 1` and `input_commitments`.
Commitments bind source/layout identity and paid component receipts; they cannot
disappear or regress on a later observation. A legacy checkpoint can be read by
the new class without a write on load. Old readers reject the new fields rather
than silently forgetting owned infrastructure. The extension composes with the
separate #29 background-job extension.

Writes use the existing atomic checkpoint path plus POSIX directory fsync.
Persistence failure poisons the current controller instance before another
mutation. Prepared/ambiguous dispatches, reservations, prior failures, and
background-job locks are preserved. A post-dispatch route fault retains the
foreground pending action for reconciliation.

Ownership/layout records reside in the existing ephemeral FLE runtime, not a
new persistent game-save service. Loss of that runtime is not recovered by
replaying a Python checkpoint. Captured ownership detects missing runtime rows;
it does not reconstruct entities or authorize replay.

Full snapshots and legacy JSONL retain route coordinates and component evidence.
Only the model-facing view removes repetitive owned belt inventories and replaces
the route layout with its state, counts, reserve, proof, and next component.
Fresh preconditions and verifiers always use the full authoritative snapshot.
This keeps large routes from needlessly exhausting the existing model byte limit.

Supervisor, diagnostics, and research-event consumers still need coordinated
integration with these named checkpoint extensions before supervised deployment.
No claim that the open research PR stack already understands them is made.

## Offline and native acceptance

```
python -m pip install -e '.[test]'
python -m pytest tests/test_input_route_contract.py tests/test_input_routes_lua.py tests/test_input_route_integration.py -q
python -m pytest tests/ -q
python -m compileall -q src tests
```

Pure tests use explicitly synthetic evidence. Lua tests execute the actual route
adapter against a synthetic engine fixture using Lupa, or LuaTeX where Lupa is
absent. Controller integration uses the real dispatcher/planner and a synthetic
backend. None of those simulations establishes real pathfinding, native geometry,
mining yield, reach, or physical throughput. Hosted CI and local subset results
must be reported separately; skipped tests are not passes.

Consulted official API references include [LuaEntity](https://lua-api.factorio.com/latest/classes/LuaEntity.html)
(`drop_target`, `mining_area`, `mining_target`, `belt_neighbours`),
[LuaEntityPrototype](https://lua-api.factorio.com/latest/classes/LuaEntityPrototype.html)
(`vector_to_place_result`, inserter vectors), and
[LuaTransportLine](https://lua-api.factorio.com/latest/classes/LuaTransportLine.html)
(`get_item_count`). The latest documentation may describe a newer engine than the
project's pinned Factorio 2.0 environment. It is not native 2.0 acceptance evidence.

Before deployment, perform independent exact-head stack review and isolated,
explicitly authorized native acceptance. Test real placement and conservation,
obstacles, reach, mid-build inventory changes, receipt loss, reconnects, fuel
starvation, full output, and depletion. Compare matched initial worlds using
verified milestone time in game ticks and wall time, useful production, manual
transfers, starvation and uncertainty rates. Do not use fewer actions alone as a
performance claim or pool runs across code-changing supervisor repairs.

## Next increments

1. Reconcile/land the parent stack and supervisor/checkpoint/evidence consumers;
   run isolated native acceptance and a pinned paired baseline comparison.
2. Add robust resource exhaustion/capacity handling and route service priorities;
   extend to electric inserters/drills and power-aware multi-furnace layouts.
3. Automate fuel supply and downstream science delivery, retaining reservations
   and sustained-flow verification rather than treating placed belts as success.
4. Compare recurring assembler production against handcrafting, and calibrate
   JEV near-milestone choices against independently measured production results.
