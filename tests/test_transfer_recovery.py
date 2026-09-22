import copy
import hashlib
import itertools
import json
import subprocess
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

import pytest

from jev_factorio.controller import HierarchicalLoop
from jev_factorio.memory import CampaignMemory
from jev_factorio.research_log import ResearchLog, RunConfiguration, canonical_bytes
from jev_factorio.skills import Plan, Step
from jev_factorio.state import GameSnapshot
from jev_factorio.telemetry import make_attempt
from jev_factorio.transfer_recovery import build_recovery, check_recovery


SESSION = "fle-transfer-recovery"
ACTOR = {"session_id": SESSION, "unit_number": 235}
MACHINE_UNIT = 334
INVENTORY = {"iron-ore": 80}
MACHINE_POSITION = {"x": 12.5, "y": -4.0}
FIXED_UTC = datetime(2026, 9, 21, tzinfo=timezone.utc)


def _machine(*, input_amount, products_finished, crafting, progress, recipe="iron-plate"):
    return {
        "unit_number": MACHINE_UNIT,
        "name": "stone-furnace",
        "position": copy.deepcopy(MACHINE_POSITION),
        "recipe": recipe,
        "input": ({"iron-ore": input_amount} if input_amount else {}),
        "crafting": crafting,
        "crafting_progress": progress,
        "products_finished": products_finished,
    }


def _state(tick, *, input_amount, products_finished, crafting, progress,
           recipe="iron-plate"):
    factory = {
        "player_bound": True,
        "player_connected": True,
        "craft_job_actor": copy.deepcopy(ACTOR),
        "crafting_queue": 0,
        "craft_job": {"id": "craft-job-1", "recipe": "iron-plate", "status": "completed"},
        "input_routes": {"sources": {}},
        "receipts": {},
        "entities": {
            "recipe:iron-plate": _machine(
                input_amount=input_amount, products_finished=products_finished,
                crafting=crafting, progress=progress, recipe=recipe,
            ),
        },
    }
    state = asdict(GameSnapshot(
        tick=tick, session_id=SESSION, world_kind="fle",
        inventory=copy.deepcopy(INVENTORY), craft_queue=[], factory=factory,
    ))
    state["player_position"] = list(state["player_position"])
    return state


def _recovery_case(tmp_path, *, initial_failures=0, after_phase="before_decision",
                   historical_change=None, extra_dispatch=False):
    parameters = {
        "role": "recipe:iron-plate", "item": "iron-ore", "quantity": 50,
        "receipt": "transfer:iron-ore:recovery",
    }
    plan = Plan(
        "iron-recovery-plan", "iron_smelting", "Recover a rejected furnace insertion",
        (Step("factory_insert", "transfer", parameters=parameters),),
    )
    pending = {"started_tick": 100, "polls": 0, "action": "factory_insert",
               "dispatch": "ambiguous"}
    memory = CampaignMemory(
        SESSION, "iron_smelting", active_goal="iron_smelting",
        active_plan=plan.to_dict(), pending=pending, last_tick=100,
    )
    memory.failures[plan.id] = initial_failures
    memory.attempt = make_attempt(
        SESSION, "iron_smelting", memory.active_plan, 0, pending,
        process_id="a" * 32, unit_number=MACHINE_UNIT,
    )
    memory.attempt["dispatch_phases"] = {
        "transfer_rpc": {
            "stage": "transfer_rpc", "status": "failed",
            "at_utc": "2026-09-21T00:00:00+00:00", "seconds": 0.1,
            "error_code": "execution",
        },
    }

    root = Path(__file__).resolve().parents[1]
    commit = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=root, text=True,
    ).strip()
    provenance = {
        "run_id": "supervised-run-1", "segment_id": "segment-1",
        "execution_id": "execution-1",
        "code_revision": {
            "commit": commit,
            "source_sha256": hashlib.sha256(
                (root / "src/jev_factorio/lua/factory.lua").read_bytes()
            ).hexdigest(),
        },
    }
    before = _state(
        100, input_amount=5, products_finished=2838,
        crafting=True, progress=0.25,
    )
    after = _state(
        110, input_amount=4, products_finished=2839,
        crafting=True, progress=0.75,
    )
    if historical_change:
        historical_change(after)
    current = GameSnapshot(
        tick=200, session_id=SESSION, world_kind="fle",
        inventory=copy.deepcopy(INVENTORY), craft_queue=[], factory={
            **copy.deepcopy(after["factory"]),
            "entities": {
                "recipe:iron-plate": _machine(
                    input_amount=0, products_finished=2844,
                    crafting=False, progress=0, recipe="",
                ),
            },
        },
    )
    action_id = "action:recovery-1"
    common = {
        "trace_id": "trace-1", "controller": "hierarchical",
        "decision_id": "decision:1", "model_call_id": None,
        "session_id": SESSION, "world_kind": "fle",
        "supervisor_provenance": provenance,
    }
    run_dir = tmp_path / "research-run"
    config = RunConfiguration(
        backend="fle", controller="hierarchical", policy="deterministic",
        target="iron_smelting", checkpoint_enabled=True,
    )
    with ResearchLog(
        run_dir, config, repo_dir=root, environ={},
        monotonic_ns=itertools.count().__next__, utc_now=lambda: FIXED_UTC,
    ) as writer:
        writer.emit(
            "observation", {
                **common, "observation_id": "observation:before",
                "action_id": None, "factorio_tick": 100,
                "phase": "before_dispatch", "snapshot": before,
            }, factorio_tick=100, session_id=SESSION,
        )
        action = {
            **common, "observation_id": "observation:before", "action_id": action_id,
            "attempt_id": memory.attempt["id"], "action": "factory_insert",
            "parameters": copy.deepcopy(parameters), "plan_id": plan.id,
            "step_index": 0, "role": "plan", "related_action_id": None,
            "checkpointed": True,
            "pending": {**pending, "dispatch": "prepared"},
            "dispatch": "prepared", "factorio_tick": 100,
        }
        writer.emit("action_prepared", action, factorio_tick=100, session_id=SESSION)
        writer.emit(
            "action_returned", {
                **action, "status": "error", "duration_ns": 1,
                "error": {"category": "execution", "http_status": None},
            }, factorio_tick=100, session_id=SESSION,
        )
        writer.emit(
            "observation", {
                **common, "observation_id": "observation:after",
                "action_id": action_id, "factorio_tick": 110,
                "phase": after_phase, "snapshot": after,
            }, factorio_tick=110, session_id=SESSION,
        )
        if extra_dispatch:
            writer.emit("action_prepared", {**action, "attempt_id": "another-attempt"},
                        factorio_tick=110, session_id=SESSION)

    proof = build_recovery(memory, run_dir)
    memory.transfer_recovery = proof
    return memory, run_dir, current, proof, plan


def test_build_and_check_recovery_requires_canonical_sealed_chain(tmp_path):
    memory, run_dir, current, proof, _ = _recovery_case(tmp_path)

    assert proof["version"] == 1
    assert proof["sequences"] == [2, 3, 4, 5]
    assert proof["audit"]["complete"] is True
    result = check_recovery(memory, current)
    assert result == {
        "attempt_id": memory.attempt["id"],
        "events_sha256": proof["events_sha256"],
        "sequences": [2, 3, 4, 5],
        "transferred_quantity": 0,
    }
    assert run_dir.joinpath("integrity.json").is_file()


@pytest.mark.parametrize("corruption", ["pending", "current", "sealed_event"])
def test_recovery_fails_closed_on_checkpoint_world_or_log_corruption(tmp_path, corruption):
    memory, run_dir, current, _, _ = _recovery_case(tmp_path)
    if corruption == "pending":
        memory.pending["dispatch"] = "returned"
    elif corruption == "current":
        current.factory["entities"]["recipe:iron-plate"]["input"] = {"iron-ore": 1}
    else:
        events_path = run_dir / "events.jsonl"
        events = [json.loads(line) for line in events_path.read_bytes().splitlines()]
        events[1]["payload"]["phase"] = "after_dispatch"
        events_path.write_bytes(b"".join(canonical_bytes(event) + b"\n" for event in events))

    assert check_recovery(memory, current) is None


@pytest.mark.parametrize("change", [
    lambda m, s: setattr(s, "session_id", "another-session"),
    lambda m, s: s.inventory.update({"iron-ore": 79}),
    lambda m, s: s.factory["craft_job_actor"].update(unit_number=999),
    lambda m, s: s.factory["entities"]["recipe:iron-plate"].update(unit_number=999),
    lambda m, s: s.factory["entities"]["recipe:iron-plate"].update(recipe="copper-plate"),
    lambda m, s: s.factory["receipts"].update({"unexpected": {"quantity": 1}}),
    lambda m, s: s.factory.update(crafting_queue=1),
    lambda m, s: s.factory["craft_job"].update(status="running"),
    lambda m, s: s.factory["input_routes"]["sources"].update(ore={}),
    lambda m, s: m.failures.update({"another-plan": 1}),
    lambda m, s: m.attempt.update(id="b" * 32),
    lambda m, s: s.factory.update(player_bound=False),
])
def test_recovery_rejects_changed_identity_effects_or_budget(tmp_path, change):
    memory, _, current, _, _ = _recovery_case(tmp_path)
    change(memory, current)
    assert check_recovery(memory, current) is None


@pytest.mark.parametrize("options", [
    {"after_phase": "after_dispatch"},
    {"extra_dispatch": True},
    {"historical_change": lambda s: s["inventory"].update({"iron-ore": 79})},
    {"historical_change": lambda s: s["factory"]["entities"]["recipe:iron-plate"].update(products_finished=2840)},
    {"historical_change": lambda s: s["factory"]["receipts"].update({"new": {"quantity": 1}})},
])
def test_sealed_but_inapplicable_history_is_not_recovery_evidence(tmp_path, options):
    with pytest.raises(ValueError, match="Transfer recovery"):
        _recovery_case(tmp_path, **options)


def test_truncated_log_cannot_recover(tmp_path):
    memory, run_dir, current, _, _ = _recovery_case(tmp_path)
    path = run_dir / "events.jsonl"
    path.write_bytes(path.read_bytes()[:-3])
    assert check_recovery(memory, current) is None


class _NoActBackend:
    def __init__(self, snapshot):
        self.snapshot = snapshot
        self.act_calls = []

    def observe(self):
        return copy.deepcopy(self.snapshot)

    def act(self, action):
        self.act_calls.append(action)
        raise AssertionError("recovery rejection must not call backend.act")


def test_controller_rejects_proven_recovery_without_act_and_consumes_failure_budget(
    monkeypatch, tmp_path,
):
    memory, _, current, _, plan = _recovery_case(tmp_path, initial_failures=1)
    backend = _NoActBackend(current)
    loop = HierarchicalLoop(
        backend, policy="deterministic", target="iron_smelting",
        checkpoint=str(tmp_path / "checkpoint.json"), tick_seconds=0,
    )
    loop.memory = memory

    record = loop.step()

    assert record["action"] == "reconcile"
    assert memory.failures[plan.id] == 2
    assert memory.transfer_recovery is None
    assert memory.active_plan is None and memory.pending is None
    assert backend.act_calls == []

    monkeypatch.setattr(
        "jev_factorio.controller.compile_plans",
        lambda goal, snapshot: ([plan], ""),
    )
    blocked = loop.step()
    assert blocked["status"] == "blocked"
    assert blocked["outcome"] == "Plan failure budget exhausted"
    assert backend.act_calls == []
