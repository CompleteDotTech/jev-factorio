"""Atomic, session-bound controller checkpoints (not Factorio save files)."""
from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .planning.materials import quantities


@dataclass
class CampaignMemory:
    session_id: str
    target: str
    version: int = 1
    active_goal: str | None = None
    completed_goals: dict[str, int] = field(default_factory=dict)
    active_plan: dict | None = None
    step_index: int = 0
    pending: dict | None = None
    reservations: dict[str, dict[str, float]] = field(default_factory=dict)
    failures: dict[str, int] = field(default_factory=dict)
    history: list[dict] = field(default_factory=list)
    last_tick: int = -1
    status: str = "running"
    reason: str = ""
    stalled_decisions: int = 0

    def event(self, kind: str, **details) -> None:
        self.history.append({"kind": kind, **details})
        self.history = self.history[-64:]

    def reserve(self, owner: str, costs: dict[str, float], inventory: dict[str, int]) -> None:
        costs = quantities(costs)
        available = quantities(inventory)
        for key, held in self.reservations.items():
            if key != owner:
                for item, amount in held.items():
                    available[item] = available.get(item, 0) - amount
        if any(amount > available.get(item, 0) for item, amount in costs.items()):
            raise ValueError("Insufficient unreserved construction materials")
        self.reservations[owner] = costs

    def release(self, owner: str) -> None:
        self.reservations.pop(owner, None)

    def save(self, path: Path | None) -> None:
        if path is None:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(asdict(self), stream, sort_keys=True, allow_nan=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    @classmethod
    def load(cls, path: Path, session_id: str, target: str) -> CampaignMemory:
        try:
            def invalid_constant(value):
                raise ValueError(f"Invalid numeric constant in checkpoint: {value}")

            data = json.loads(path.read_text(encoding="utf-8"), parse_constant=invalid_constant)
            memory = cls(**data)
            if (memory.version != 1 or memory.session_id != session_id
                    or memory.target != target or not session_id):
                raise ValueError("Checkpoint version, session, or target mismatch")
            if (type(memory.last_tick) is not int or type(memory.step_index) is not int
                    or memory.step_index < 0 or not isinstance(memory.history, list)
                    or not isinstance(memory.completed_goals, dict)
                    or not isinstance(memory.failures, dict)
                    or memory.status not in {"running", "completed", "blocked", "uncertain"}):
                raise ValueError("Invalid checkpoint state")
            from .planning.goals import goal_order
            order = goal_order(target)
            if (memory.last_tick < -1 or memory.active_goal not in [None, *order]
                    or not set(memory.completed_goals).issubset(order)
                    or any(type(t) is not int or not 0 <= t <= memory.last_tick
                           for t in memory.completed_goals.values())
                    or any(type(n) is not int or n < 0 for n in memory.failures.values())
                    or type(memory.stalled_decisions) is not int or memory.stalled_decisions < 0
                    or len(memory.history) > 64 or not all(isinstance(e, dict) for e in memory.history)):
                raise ValueError("Invalid checkpoint receipts or counters")
            for costs in memory.reservations.values():
                quantities(costs)
            if memory.active_plan is not None:
                # Import locally to avoid a memory/skill dependency cycle.
                from .skills import Plan
                plan = Plan.from_dict(memory.active_plan)
                if plan.goal != memory.active_goal or memory.step_index >= len(plan.steps):
                    raise ValueError("Checkpoint step outside plan")
            if memory.pending is not None and memory.active_plan is None:
                raise ValueError("Pending action without an active plan")
            if memory.pending is not None:
                pending = memory.pending
                if (not isinstance(pending, dict)
                        or set(pending) != {"started_tick", "polls", "action", "dispatch"}
                        or type(pending["started_tick"]) is not int
                        or not 0 <= pending["started_tick"] <= memory.last_tick
                        or type(pending["polls"]) is not int or pending["polls"] < 0
                        or pending["action"] != plan.steps[memory.step_index].action
                        or pending["dispatch"] not in {"prepared", "ambiguous", "returned"}):
                    raise ValueError("Invalid pending action in checkpoint")
            return memory
        except (TypeError, KeyError, AttributeError, json.JSONDecodeError) as error:
            raise ValueError("Invalid controller checkpoint; refusing to reset it") from error
