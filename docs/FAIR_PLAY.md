# Native player-action fairness

The FLE backend now routes movement, harvesting, placement, and connection
construction through repository-owned `FairActions`. Disabling FLE fast mode
alone was insufficient: its mining and connection tools contained other
shortcuts.

- Walking uses the original bound LuaPlayer, the engine pathfinder, and
  `walking_state` on actual game ticks. It does not relocate the character.
- Mining selects the native resource and holds `mining_state`; inventory gains
  must come from the engine. Depleted targets can be approached and replaced.
  Reachable resources are mined in place; otherwise the walking approach is on
  the actor-facing side rather than beyond the resource. Wood failure budgets
  use the observed surface, tree prototype and exact position because native
  trees need not have a `unit_number`. A replacement at the same site retains
  that site's failure budget.
- Placement transfers an existing inventory stack into the cursor and uses
  `can_build_from_cursor` / `build_from_cursor`. It verifies an actual item debit
  and returns remaining cursor items. Obstructions are not removed by relocation.
- Transfers, machine configuration, and silo interaction check native player
  reach. Research selection remains a normal remotely accessible player action.
- Pipes and poles follow a bounded collision-checked placement route and use the
  same walking/building primitives. Native fluid/electric topology still decides
  whether the connection succeeded; the command alone is not success evidence.
  Pipe routes start and end at the nearest compatible FLE-observed fluid-handler
  connection cells, including generic offshore-pump, boiler, and generator ports.
- The adapter rejects disconnected/replaced characters, cheat mode, and game
  speed other than 1. A short renewed control lease stops input if the controller
  disappears. Path, progress, and observation timeouts fail closed.
- Legacy FLE walking/mining callbacks are quarantined before attachment. Retained
  scripted work is preserved but rejected until explicitly reconciled.

These APIs emulate player controls rather than keyboard/mouse events. Factorio's
[LuaControl documentation](https://lua-api.factorio.com/latest/classes/LuaControl.html#mining_state)
explains why mining must also select the intended entity.

The existing dedicated FLE scenario remains peaceful and retains its original
initialization and starter layout. This change does not retroactively make
earlier accelerated progress a fair run, undo the existing factory, or claim a
vanilla-from-new-game benchmark. Full rocket completion remains unverified.

The autonomous supervisor's repair prompt carries these requirements forward:
fix issues, test, obtain independent exact-head review, publish/merge/synchronize,
and only then resume. It must not repair progress by reintroducing shortcuts.
Prompt requirements are not a substitute for runtime guards or native evidence.

Tests include mocked Lua control/reach/lease/cursor behavior and bounded route
planning. Native acceptance must separately sample displacement over game ticks,
observe engine-produced mining yields, and verify inventory-paid building.

Native integration on Factorio 2.0.77 verified walking across sampled real ticks,
ordinary coal and stone mining, hand-crafting, and inventory-paid furnace
placement in the existing session. A paused copper transfer was reconciled
against its original entity/receipt without clearing the controller's pending
action. These checks do not establish a complete native pipe/pole campaign or
rocket completion.
