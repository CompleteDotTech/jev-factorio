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
    def __init__(self, backend, jev=None, *, target: str = "rocket_launch",
                 policy: str = "jev", checkpoint: str | None = None,
                 resume_controller: bool = False, confidence_floor: float = 0.45,
                 tick_seconds: float = 2.0, log_file: str | None = None,
                 max_request_bytes: int = 32000, max_pending_polls: int = 32,
                 max_stalled_decisions: int = 4):
        if policy not in {"jev", "deterministic"}:
            raise ValueError("Unknown campaign policy")
        if policy == "jev" and jev is None:
            raise ValueError("Supply an explicit Jev client; mock use must be intentional")
        if not math.isfinite(confidence_floor) or not 0 <= confidence_floor <= 1:
            raise ValueError("Confidence floor must be in [0, 1]")
        if not math.isfinite(tick_seconds) or tick_seconds < 0:
            raise ValueError("Invalid decision interval")
        if min(max_request_bytes, max_pending_polls, max_stalled_decisions) < 1:
            raise ValueError("Controller budgets must be positive")
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

    @property
    def terminal(self) -> bool:
        return self.memory is not None and self.memory.status in {"completed", "blocked", "uncertain"}

    def _observe(self) -> GameSnapshot:
        snapshot = self.backend.observe()
        if not snapshot.session_id or snapshot.world_kind not in {"mock", "fle"}:
            raise ValueError("Hierarchical control requires identified backend/session telemetry")
        if self.policy == "jev" and getattr(self.jev, "is_mock", False) and snapshot.world_kind != "mock":
            raise ValueError("A mock model cannot control or benchmark a live backend")
        if type(snapshot.tick) is not int or snapshot.tick < 0:
            raise ValueError("Invalid observation tick")
        # Validate serialized facts rather than allowing NaN into conditions.
        json.dumps(snapshot.for_jev(), allow_nan=False)
        if self.memory is None:
            self.memory = (CampaignMemory.load(self.checkpoint, snapshot.session_id, self.target)
                           if self.resume_controller else CampaignMemory(snapshot.session_id, self.target))
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
        if self.log_file:
            self.log_file.parent.mkdir(parents=True, exist_ok=True)
            with self.log_file.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(_json_safe(record), allow_nan=False) + "\n")
        print(f"[t={before.tick}] {self.memory.status}: {action} -> {outcome}", flush=True)
        return record

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
        pending["polls"] += 1
        expired = (snapshot.tick - pending["started_tick"] >= step.timeout_ticks
                   or pending["polls"] >= self.max_pending_polls)
        if expired:
            if step.action == "idle":
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
            plans, blocker = compile_plans(self.memory.active_goal, snapshot)
            plans = [p for p in plans if self.memory.failures.get(p.id, 0) < 2]
            if not plans:
                self.memory.status, self.memory.reason = "blocked", blocker or "Plan failure budget exhausted"
                return self._record(snapshot, "observe", self.memory.reason)
            if self.policy == "deterministic":
                chosen = min(plans, key=lambda p: (len(p.steps), p.id))
                self._decision = Decision(chosen.id, "deterministic")
            else:
                state = {"facts": snapshot.for_jev(), "active_goal": asdict(GOALS[self.memory.active_goal]),
                         "history": self.memory.history[-8:]}
                try:
                    self._decision = select_plan(self.jev, state, plans, self.confidence_floor,
                                                 self.max_request_bytes)
                except ValueError as error:
                    self._decision = Decision(None, "observe", str(error))
                chosen = next((p for p in plans if p.id == self._decision.plan_id), None)
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
        plan = Plan.from_dict(self.memory.active_plan)
        index = plan.next_step(fresh, self.memory.step_index)
        if index == len(plan.steps):
            self._clear_plan()
            self._refresh_goals(fresh)
            return self._record(snapshot, "verify", "Plan effects already observed", fresh, True)
        self.memory.step_index = index
        step = plan.steps[index]
        if not step.allowed(fresh):
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
            outcome = self.backend.act(step.action)
        except Exception as error:
            self.memory.pending["dispatch"] = "ambiguous"
            self.memory.event("dispatch_error", error_type=type(error).__name__, tick=fresh.tick)
            return self._record(snapshot, step.action, "Ambiguous dispatch; verification required", fresh)
        self.memory.pending["dispatch"] = "returned"
        self._save()
        after = self._observe()  # On failure, pending remains durable for the next iteration.
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
