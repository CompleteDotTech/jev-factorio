"""Opt-in hierarchical control: commit, execute, observe, verify, and recover.

The inherited run loop retains the existing monotonic deadline and transient
HTTP-failure handling. No live-game or model-performance claims follow from
passing the offline tests.
"""
from __future__ import annotations

import json
import math
from dataclasses import asdict
from pathlib import Path

from .judgments import Decision, select_plan
from .loop import AgentLoop
from .memory import CampaignMemory
from .planning.goals import GOALS, completed, goal_order
from .skills import Plan, compile_plans
from .state import GameSnapshot
from .provenance import gameplay_context


def _json_safe(value):
    """Keep malformed numeric answers auditable without emitting invalid JSON."""
    if isinstance(value, float) and not math.isfinite(value):
        return {"invalid_numeric": repr(value)}
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return value


class HierarchicalLoop(AgentLoop):
    memory_type = CampaignMemory

    def __init__(self, backend, jev=None, *, target: str = "rocket_launch",
                 policy: str = "jev", checkpoint: str | None = None,
                 resume_controller: bool = False, confidence_floor: float = 0.45,
                 tick_seconds: float = 2.0, log_file: str | None = None,
                 max_request_bytes: int = 32000, max_pending_polls: int = 32,
                 max_stalled_decisions: int = 4, factory_scheduling: str = "serial"):
        if factory_scheduling not in {"serial", "ready-work"}:
            raise ValueError("Unknown factory scheduling policy")
        self.factory_scheduling = factory_scheduling
        if policy not in {"jev", "deterministic", "hybrid"}:
            raise ValueError("Unknown campaign policy")
        if policy != "deterministic" and jev is None:
            raise ValueError("Supply an explicit Jev client; mock use must be intentional")
        if not math.isfinite(confidence_floor) or not 0 <= confidence_floor <= 1:
            raise ValueError("Confidence floor must be in [0, 1]")
        if not math.isfinite(tick_seconds) or tick_seconds < 0:
            raise ValueError("Invalid decision interval")
        if min(max_request_bytes, max_pending_polls, max_stalled_decisions) < 1:
            raise ValueError("Controller budgets must be positive")
        self.provenance = gameplay_context()
        self.order = goal_order(target)
        self.backend, self.jev, self.policy = backend, jev, policy
        self.target, self.confidence_floor = target, confidence_floor
        self.tick_seconds = tick_seconds
        self.log_file = Path(log_file) if log_file else None
        self.checkpoint = Path(checkpoint) if checkpoint else None
        self.resume_controller = resume_controller
        if resume_controller and (self.checkpoint is None or not self.checkpoint.is_file()):
            raise ValueError("Resuming requires an existing controller checkpoint")
        if self.checkpoint and self.checkpoint.exists() and not resume_controller:
            raise ValueError("Checkpoint exists; explicitly resume or use a new path")
        self.max_request_bytes = max_request_bytes
        self.max_pending_polls = max_pending_polls
        self.max_stalled_decisions = max_stalled_decisions
        self.memory: CampaignMemory | None = None
        self._decision: Decision | None = None
        self.catalog = None
        if target in {"rocket_launch", "iron_smelting", "steam_power", "automation_science"} \
                and hasattr(backend, "enable_factory"):
            self.catalog = backend.enable_factory()
            self.max_pending_polls = max(self.max_pending_polls, 1800)

    @property
    def terminal(self) -> bool:
        return self.memory is not None and self.memory.status in {"completed", "blocked", "uncertain"}

    def _observe(self) -> GameSnapshot:
        snapshot = self.backend.observe()
        if not snapshot.session_id or snapshot.world_kind not in {"mock", "fle"}:
            raise ValueError("Hierarchical control requires identified backend/session telemetry")
        if self.policy != "deterministic" and getattr(self.jev, "is_mock", False) and snapshot.world_kind != "mock":
            raise ValueError("A mock model cannot control or benchmark a live backend")
        if type(snapshot.tick) is not int or snapshot.tick < 0:
            raise ValueError("Invalid observation tick")
        # Validate serialized facts rather than allowing NaN into conditions.
        json.dumps(snapshot.for_jev(), allow_nan=False)
        if self.memory is None:
            self.memory = (self.memory_type.load(self.checkpoint, snapshot.session_id, self.target)
                           if self.resume_controller else self.memory_type(snapshot.session_id, self.target))
        if self.memory.session_id != snapshot.session_id or snapshot.tick < self.memory.last_tick:
            raise ValueError("Session changed or observation tick regressed; refusing to act")
        self.memory.last_tick = snapshot.tick
        return snapshot

    def _save(self) -> None:
        self.memory.save(self.checkpoint)

    def _clear_plan(self) -> None:
        if self.memory.active_plan:
            self.memory.release(self.memory.active_plan["id"])
        self.memory.active_plan = None
        self.memory.pending = None
        self.memory.step_index = 0

    def _fail_plan(self, reason: str) -> None:
        key = self.memory.active_plan["id"]
        self.memory.failures[key] = self.memory.failures.get(key, 0) + 1
        self.memory.event("plan_failed", plan=key, reason=reason, tick=self.memory.last_tick)
        self.memory.reason = reason
        self._clear_plan()
        self._save()

    def _refresh_goals(self, snapshot: GameSnapshot) -> None:
        for key in self.order:
            if key not in self.memory.completed_goals and all(
                dep in self.memory.completed_goals for dep in GOALS[key].prerequisites
            ) and completed(key, snapshot):
                self.memory.completed_goals[key] = snapshot.tick
                self.memory.event("goal_completed", goal=key, tick=snapshot.tick,
                                  world_kind=snapshot.world_kind)
        if self.target in self.memory.completed_goals:
            self.memory.status, self.memory.reason = "completed", "Verified target milestone"
            self._clear_plan()
            return
        goal = next(key for key in self.order if key not in self.memory.completed_goals)
        if self.memory.active_goal != goal:
            self._clear_plan()
            self.memory.active_goal = goal
            self.memory.event("goal_activated", goal=goal, tick=snapshot.tick)

    def _record(self, before: GameSnapshot, action: str, outcome: str,
                after: GameSnapshot | None = None, verified: bool = False) -> dict:
        self._save()
        decision = self._decision
        record = {
            **self.provenance,
            "schema_version": 1, "controller": "hierarchical", "policy": self.policy,
            "tick": before.tick, "session_id": before.session_id,
            "world_kind": before.world_kind, "goal": self.memory.active_goal,
            "target": self.target, "status": self.memory.status, "reason": self.memory.reason,
            "action": action, "outcome": outcome, "verified": verified,
            "state": before.for_jev(), "after_state": (after or before).for_jev(),
            "completed_goals": dict(self.memory.completed_goals),
            "decision": asdict(decision) if decision else None,
            "model_call": decision is not None and decision.model_called,
            "requested_model": getattr(self.jev, "model", None),
            "resolved_model": getattr(self.jev, "last_model", None) if decision else None,
            "usage": getattr(self.jev, "last_usage", None) if decision else None,
            "pending": self.memory.pending, "history": self.memory.history[-8:],
        }
        if getattr(self, "factory_scheduling", "serial") != "serial":
            record["factory_scheduling"] = self.factory_scheduling
        fair = getattr(getattr(self, "backend", None), "_fair", None)
        metrics = getattr(fair, "metrics", None)
        if isinstance(metrics, dict):
            record["fair_action_metrics"] = dict(metrics)
        record.update(self._record_extras())
        if self.log_file:
            self.log_file.parent.mkdir(parents=True, exist_ok=True)
            with self.log_file.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(_json_safe(record), allow_nan=False) + "\n")
        print(f"[t={before.tick}] {self.memory.status}: {action} -> {outcome}", flush=True)
        return record

    def _model_facts(self, snapshot: GameSnapshot) -> dict:
        return snapshot.for_jev()

    def _record_extras(self) -> dict:
        return {}

    def _step_allowed(self, step, snapshot: GameSnapshot) -> bool:
        return step.allowed(snapshot)

    def _execution_barrier(self, snapshot: GameSnapshot) -> bool:
        return False

    def _absent_ambiguous_placement(self, plan: Plan, step, snapshot: GameSnapshot) -> bool:
        """Prove that retrying an ambiguous placement cannot duplicate a building."""
        pending = self.memory.pending or {}
        if pending.get("dispatch") != "ambiguous" or step.action != "factory_place":
            return False
        parameters = step.parameters or {}
        role, name = parameters.get("role"), parameters.get("name")
        entities = snapshot.factory.get("entities", {})
        counts = snapshot.factory.get("force_entity_counts")
        costs = step.costs or {}
        reserved = self.memory.reservations.get(plan.id)
        return bool(
            role and name and role not in entities
            and isinstance(counts, dict) and counts.get(name, 0) == 0
            and costs and reserved == costs
            and all(snapshot.inventory.get(item, 0) >= quantity
                    for item, quantity in costs.items())
        )

    def _absent_ambiguous_connection(self, plan: Plan, step, snapshot: GameSnapshot) -> bool:
        """Prove an ambiguous connection placed no connector before permitting a replan."""
        pending = self.memory.pending or {}
        if pending.get("dispatch") != "ambiguous" or step.action != "factory_connect":
            return False
        parameters = step.parameters or {}
        source, target, kind = (
            parameters.get("source"), parameters.get("target"), parameters.get("kind")
        )
        entities = snapshot.factory.get("entities", {})
        counts = snapshot.factory.get("force_entity_counts")
        costs = step.costs or {}
        reserved = self.memory.reservations.get(plan.id)
        count = counts.get(kind, 0) if isinstance(counts, dict) else None
        retained = all(snapshot.inventory.get(item, 0) >= quantity
                       for item, quantity in costs.items())
        return bool(
            source in entities and target in entities and kind in {"pipe", "small-electric-pole"}
            and isinstance(counts, dict)
            and type(count) is int and count >= 0
            and set(costs) == {kind} and reserved == costs
            and retained and count == 0
        )

    def _partial_unacknowledged_gather(self, step, snapshot: GameSnapshot) -> bool:
        """Identify a partial native gather without treating it as step success.

        An inventory increase below the committed threshold is not a license to
        replay an unacknowledged harvest.  The resumed controller instead fails
        this plan and replans from the observed inventory.  This is deliberately
        narrower than normal inventory verification: it applies only after an
        prepared or ambiguous factory gather has already been observed at least
        once.
        """
        pending = self.memory.pending or {}
        parameters = step.parameters or {}
        item = parameters.get("resource")
        observed = snapshot.inventory.get(item, 0) if isinstance(item, str) else 0
        return bool(
            pending.get("dispatch") in {"prepared", "ambiguous"}
            and type(pending.get("polls")) is int and pending["polls"] > 0
            and step.action == "factory_gather" and step.effect == "inventory"
            and item == step.item and item
            and isinstance(observed, (int, float)) and not isinstance(observed, bool)
            and math.isfinite(observed) and 0 < observed < step.threshold
        )

    def _verify_pending(self, snapshot: GameSnapshot) -> dict:
        plan = Plan.from_dict(self.memory.active_plan)
        step = plan.steps[self.memory.step_index]
        pending = self.memory.pending
        if step.satisfied(snapshot):
            self.memory.status, self.memory.reason = "running", ""
            self.memory.release(plan.id)
            self.memory.pending = None
            self.memory.step_index += 1
            self.memory.stalled_decisions = 0
            self.memory.event("step_verified", plan=plan.id, action=step.action, tick=snapshot.tick)
            if self.memory.step_index == len(plan.steps):
                self._clear_plan()
            self._refresh_goals(snapshot)
            return self._record(snapshot, "verify", "Observed expected postcondition", verified=True)
        if self._absent_ambiguous_placement(plan, step, snapshot):
            name = step.parameters["name"]
            reason = (f"Observed no durable {name} placement and retained all reserved "
                      "materials; replan without replaying the ambiguous dispatch")
            self.memory.status = "running"
            self._fail_plan(reason)
            return self._record(snapshot, "reconcile", reason)
        if self._absent_ambiguous_connection(plan, step, snapshot):
            kind = step.parameters["kind"]
            reason = (f"Observed no durable {kind} construction and retained all reserved "
                      "materials; replan without replaying the ambiguous dispatch")
            self.memory.status = "running"
            self._fail_plan(reason)
            return self._record(snapshot, "reconcile", reason)
        if self._partial_unacknowledged_gather(step, snapshot):
            quantity = snapshot.inventory[step.item]
            reason = (
                f"Observed {quantity} {step.item} below committed inventory threshold "
                f"{step.threshold} after an unacknowledged native gather; replan without "
                "replaying the ambiguous dispatch"
            )
            self.memory.status = "running"
            self._fail_plan(reason)
            return self._record(snapshot, "reconcile", reason)
        boiler = snapshot.factory.get("entities", {}).get("utility:boiler", {})
        if step.action == "factory_wait" and boiler and boiler.get("fuel", {}).get("coal", 0) < 5:
            self.memory.event("maintenance_required", reason="boiler fuel", tick=snapshot.tick)
            self._clear_plan()
            return self._record(snapshot, "observe", "Replan a nonmutating wait to replenish boiler fuel")
        if (self.factory_scheduling == "ready-work" and self.catalog is not None
                and self.memory.status == "running" and pending.get("dispatch") == "returned"
                and step.action == "factory_wait" and step.effect == "machine_output"):
            candidates, _ = self._compile_candidates(snapshot)
            ready = [candidate for candidate in candidates
                     if self.memory.failures.get(candidate.id, 0) < 2
                     and candidate.steps[0].action != "factory_wait"
                     and candidate.steps[0].allowed(snapshot)
                     and not candidate.steps[0].satisfied(snapshot)]
            if ready:
                self.memory.event("passive_wait_yielded", plan=plan.id,
                                  candidates=[candidate.id for candidate in ready], tick=snapshot.tick)
                self._clear_plan()
                return self._record(snapshot, "observe", "Yield passive machine wait to ready work")
        pending["polls"] += 1
        expired = (snapshot.tick - pending["started_tick"] >= step.timeout_ticks
                   or pending["polls"] >= self.max_pending_polls)
        if expired:
            if step.action in {"idle", "factory_wait"}:
                self._fail_plan("Production made no verified progress within the observation budget")
                return self._record(snapshot, "observe", self.memory.reason)
            # Execution may have partially mutated the game. Do not automatically
            # replay a non-idempotent command after lost acknowledgement.
            self.memory.status = "uncertain"
            self.memory.reason = "Unverified action outcome; inspect/reconcile before another mutation"
            return self._record(snapshot, "observe", self.memory.reason)
        # In a real backend observation allows game time to elapse naturally.
        # The explicitly synthetic backend advances only when given idle.
        if snapshot.world_kind == "mock":
            self.backend.act("idle")
        return self._record(snapshot, "observe", "Waiting for the in-flight postcondition")

    def _compile_candidates(self, snapshot: GameSnapshot) -> tuple[list[Plan], str]:
        if self.catalog is not None and self.memory.active_goal in {
            "rocket_launch", "iron_smelting", "steam_power", "automation_science", "bootstrap_mining"
        }:
            if self.factory_scheduling == "ready-work":
                from .planning.ready_work import compile_ready_factory

                return compile_ready_factory(self.memory.active_goal, snapshot, self.catalog)
            from .planning.factory import compile_factory

            return compile_factory(self.memory.active_goal, snapshot, self.catalog)
        return compile_plans(self.memory.active_goal, snapshot)

    def _fallback_plan(self, plans: list[Plan]) -> Plan:
        if self.factory_scheduling == "ready-work" and self.catalog is not None:
            return plans[0]  # Preserve the compiler's critical-prerequisite priority.
        return min(plans, key=lambda plan: (len(plan.steps), plan.id))

    def step(self) -> dict:
        self._decision = None
        snapshot = self._observe()
        if self.memory.status == "uncertain" and self.memory.pending:
            return self._verify_pending(snapshot)
        if self.terminal:
            return self._record(snapshot, "observe", self.memory.reason)
        # Resolve in-flight work before processing model requests or goal changes.
        if self.memory.pending:
            return self._verify_pending(snapshot)
        self._refresh_goals(snapshot)
        if self.terminal:
            return self._record(snapshot, "observe", self.memory.reason, verified=True)
        if self.memory.active_plan is None:
            plans, blocker = self._compile_candidates(snapshot)
            plans = [p for p in plans if self.memory.failures.get(p.id, 0) < 2]
            if not plans:
                self.memory.status, self.memory.reason = "blocked", blocker or "Plan failure budget exhausted"
                return self._record(snapshot, "observe", self.memory.reason)
            if self.policy == "deterministic":
                chosen = self._fallback_plan(plans)
                self._decision = Decision(chosen.id, "deterministic")
            else:
                facts = self._model_facts(snapshot)
                if facts["factory"]:
                    receipts = facts["factory"].pop("receipts", {})
                    facts["factory"].pop("connectors", None)
                    facts["factory"]["native_transfer_receipt_count"] = len(receipts)
                state = {"facts": facts, "active_goal": asdict(GOALS[self.memory.active_goal]),
                         "history": self.memory.history[-8:]}
                if self.factory_scheduling == "ready-work":
                    state["production_scheduling"] = {
                        "objective": "Advance the next production batch identified in plan descriptions",
                        "guidance": "Prefer useful work while machines run; avoid tiny pickups and idle waits",
                        "ultimate_goal": self.memory.active_goal,
                    }
                try:
                    self._decision = select_plan(self.jev, state, plans, self.confidence_floor,
                                                 self.max_request_bytes)
                except ValueError as error:
                    self._decision = Decision(None, "observe", str(error))
                chosen = next((p for p in plans if p.id == self._decision.plan_id), None)
                if chosen is None and self.policy == "hybrid":
                    chosen = self._fallback_plan(plans)
                    self._decision.plan_id = chosen.id
                    self._decision.source = "deterministic-fallback"
                if chosen is None:
                    self.memory.stalled_decisions += 1
                    self.memory.reason = self._decision.reason
                    if self.memory.stalled_decisions >= self.max_stalled_decisions:
                        self.memory.status = "blocked"
                    return self._record(snapshot, "observe", self.memory.reason)
            self.memory.active_plan = chosen.to_dict()
            self.memory.step_index = 0
            self.memory.event("plan_committed", plan=chosen.id, source=self._decision.source,
                              tick=snapshot.tick)
            self._save()

        # The world can change while a remote model evaluates the old snapshot.
        fresh = self._observe()
        if self._execution_barrier(fresh):
            return self._record(snapshot, "observe", self.memory.reason, fresh)
        plan = Plan.from_dict(self.memory.active_plan)
        index = plan.next_step(fresh, self.memory.step_index)
        if index == len(plan.steps):
            self._clear_plan()
            self._refresh_goals(fresh)
            return self._record(snapshot, "verify", "Plan effects already observed", fresh, True)
        self.memory.step_index = index
        step = plan.steps[index]
        if not self._step_allowed(step, fresh):
            self._fail_plan("Plan precondition changed; replan from current observations")
            return self._record(snapshot, "observe", self.memory.reason, fresh)
        try:
            self.memory.reserve(plan.id, step.costs or {}, fresh.inventory)
        except ValueError as error:
            self._fail_plan(str(error))
            return self._record(snapshot, "observe", str(error), fresh)
        self.memory.pending = {"started_tick": fresh.tick, "polls": 0,
                               "action": step.action, "dispatch": "prepared"}
        # Write-ahead checkpoint: after a crash even a prepared command is
        # treated as potentially dispatched, never blindly replayed.
        self._save()
        try:
            outcome = (self.backend.execute(step.action, step.parameters or {})
                       if step.action.startswith("factory_") else self.backend.act(step.action))
        except Exception as error:
            self.memory.pending["dispatch"] = "ambiguous"
            self.memory.event("dispatch_error", error_type=type(error).__name__, tick=fresh.tick)
            return self._record(snapshot, step.action, "Ambiguous dispatch; verification required", fresh)
        self.memory.pending["dispatch"] = "returned"
        self._save()
        after = self._observe()  # On failure, pending remains durable for the next iteration.
        if self._execution_barrier(after):
            return self._record(snapshot, step.action,
                                str(outcome) + "; pending retained for reconciliation", after)
        verified = step.satisfied(after)
        if verified:
            self.memory.release(plan.id)
            self.memory.pending = None
            self.memory.step_index += 1
            self.memory.stalled_decisions = 0
            self.memory.event("step_verified", plan=plan.id, action=step.action, tick=after.tick)
            if self.memory.step_index == len(plan.steps):
                self._clear_plan()
            self._refresh_goals(after)
        return self._record(snapshot, step.action, str(outcome), after, verified)
