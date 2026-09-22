"""Explicit, read-only evidence for a rejected furnace insertion.

A missing receipt alone never authorizes recovery. This opt-in reference binds a
sealed research run and the retained checkpoint; the controller rechecks both
the historical conservation proof and the current native observation. It never
executes a transfer or manufactures a native receipt.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
from dataclasses import asdict
from pathlib import Path

from .research_log import verify_run


def _require(condition, reason):
    if not condition:
        raise ValueError("Transfer recovery: " + reason)


def _hash(raw):
    return hashlib.sha256(raw).hexdigest()


def _binding(memory):
    data = asdict(memory)
    # Observation advances last_tick before verification. Status/reason may be
    # changed by the supervisor; every other retained field must be identical.
    for key in ("transfer_recovery", "last_tick", "status", "reason"):
        data.pop(key, None)
    return _hash(json.dumps(data, sort_keys=True, allow_nan=False).encode())


def _identity(snapshot, session, actor, machine, inventory, receipts, job, *, exhausted=False):
    factory = snapshot["factory"]
    entity = factory["entities"]["recipe:iron-plate"]
    _require(snapshot["session_id"] == session and snapshot["world_kind"] == "fle",
             "session or backend differs")
    _require(factory["player_bound"] is True and factory["player_connected"] is True,
             "original player is not bound")
    _require(factory["craft_job_actor"] == actor and actor["session_id"] == session
             and type(actor["unit_number"]) is int and actor["unit_number"] > 0,
             "actor differs")
    _require(all(entity[k] == machine[k] for k in
                 ("unit_number", "name", "position")), "machine differs")
    _require(entity["recipe"] == machine["recipe"] or
             (exhausted and entity["recipe"] == "" and entity["input"] == {}
              and entity["crafting"] is False and entity["crafting_progress"] == 0),
             "machine recipe differs")
    _require(snapshot["inventory"] == inventory and factory["receipts"] == receipts,
             "inventory or receipts differ")
    _require(not snapshot["craft_queue"] and factory["crafting_queue"] == 0
             and factory["craft_job"] == job and job.get("status") == "completed",
             "craft work could alter inventory")
    _require(factory["input_routes"]["sources"] == {}, "automatic input route exists")
    return entity


def _historical(memory, run_dir):
    attempt, pending = memory.attempt, memory.pending
    plan = memory.active_plan
    step = plan["steps"][memory.step_index]
    parameters = step["parameters"]
    _require(attempt["origin"] == "new" and attempt["action"] == "factory_insert"
             and pending["action"] == "factory_insert" and pending["dispatch"] == "ambiguous"
             and step["action"] == "factory_insert" and step["effect"] == "transfer",
             "not an original ambiguous insertion")
    _require(parameters["role"] == "recipe:iron-plate"
             and parameters["item"] == "iron-ore" and type(parameters["quantity"]) is int
             and parameters["quantity"] == 50, "outside supported furnace recovery")
    _require(attempt["plan_id"] == plan["id"] and attempt["step_index"] == memory.step_index
             and attempt["receipt"] == parameters["receipt"]
             and attempt["dispatch_phases"]["transfer_rpc"]["status"] == "failed"
             and attempt["observation_error"] is None, "attempt binding differs")
    audit = verify_run(run_dir)
    manifest_raw = (run_dir / "manifest.json").read_bytes()
    events_raw = (run_dir / "events.jsonl").read_bytes()
    events = [json.loads(line) for line in events_raw.splitlines()]
    prepared = [i for i, event in enumerate(events)
                if event["event_type"] == "action_prepared"
                and event["payload"].get("attempt_id") == attempt["id"]]
    _require(len(prepared) == 1, "missing or duplicate prepared action")
    index = prepared[0]
    action = events[index]["payload"]
    for key, expected in (("session_id", memory.session_id), ("plan_id", plan["id"]),
                          ("step_index", memory.step_index), ("parameters", parameters),
                          ("factorio_tick", attempt["started_tick"]),
                          ("action", "factory_insert"), ("checkpointed", True)):
        _require(action.get(key) == expected, "prepared action differs: " + key)
    before_index = max(i for i in range(index) if events[i]["event_type"] == "observation")
    before_event = events[before_index]["payload"]
    _require(before_event["phase"] == "before_dispatch"
             and before_event["observation_id"] == action["observation_id"],
             "not a genuine pre-dispatch observation")
    returned = [i for i in range(index + 1, len(events))
                if events[i]["event_type"] == "action_returned"
                and events[i]["payload"].get("attempt_id") == attempt["id"]]
    _require(len(returned) == 1, "missing or duplicate action return")
    returned_index = returned[0]
    result = events[returned_index]["payload"]
    _require(result["status"] == "error" and all(result.get(k) == action.get(k)
             for k in ("action_id", "attempt_id", "plan_id", "step_index", "parameters",
                       "session_id", "supervisor_provenance")), "action return differs")
    after_index = next(i for i in range(returned_index + 1, len(events))
                       if events[i]["event_type"] == "observation")
    after_event = events[after_index]["payload"]
    _require(after_event["phase"] == "before_decision", "post-action fallback is not evidence")
    _require(not any(e["event_type"] == "action_prepared" for i, e in enumerate(events)
                     if i > before_index and i != index), "another action could mask effects")
    revision = action["supervisor_provenance"]["code_revision"]
    commit = revision["commit"]
    _require(isinstance(commit, str) and len(commit) == 40
             and all(char in "0123456789abcdef" for char in commit), "invalid source commit")
    _require(before_event["supervisor_provenance"]["code_revision"] == revision
             and after_event["supervisor_provenance"]["code_revision"] == revision,
             "source revision differs across observations")
    # Require the exact incident Lua to match the deployed transfer contract.
    root = Path(__file__).resolve().parents[2]
    lua_path = "src/jev_factorio/lua/factory.lua"
    original = subprocess.run(["git", "show", commit + ":" + lua_path],
                              cwd=root, check=True, capture_output=True, timeout=10).stdout
    _require(original == (root / lua_path).read_bytes(), "native transfer source changed")
    before, after = before_event["snapshot"], after_event["snapshot"]
    _require(before["tick"] == attempt["started_tick"] < after["tick"], "invalid observation ticks")
    factory = before["factory"]
    machine = factory["entities"]["recipe:iron-plate"]
    actor, inventory, receipts = factory["craft_job_actor"], before["inventory"], factory["receipts"]
    _require(machine["unit_number"] == attempt["expected_unit_number"]
             and machine["name"] == "stone-furnace" and machine["recipe"] == "iron-plate",
             "not the original iron furnace")
    _require(parameters["receipt"] not in receipts and inventory["iron-ore"] >= 50,
             "receipt exists or reserved source is absent")
    job = factory["craft_job"]
    _identity(before, memory.session_id, actor, machine, inventory, receipts, job)
    end_machine = _identity(after, memory.session_id, actor, machine, inventory, receipts, job)
    _require(machine["input"] == {"iron-ore": 5}
             and end_machine["input"] == {"iron-ore": 4}
             and machine["crafting"] is True and 0 < machine["crafting_progress"] < 1
             and end_machine["crafting"] is True and 0 < end_machine["crafting_progress"] < 1
             and end_machine["products_finished"] == machine["products_finished"] + 1,
             "target change is not exactly one ordinary smelt")
    # Rehash after verification/read to reject a concurrently replaced log.
    _require(verify_run(run_dir) == audit
             and (run_dir / "manifest.json").read_bytes() == manifest_raw
             and (run_dir / "events.jsonl").read_bytes() == events_raw, "evidence changed while reading")
    proof = {"version": 1, "run_dir": str(run_dir.resolve()), "checkpoint_sha256": _binding(memory),
             "manifest_sha256": _hash(manifest_raw), "events_sha256": _hash(events_raw),
             "audit": audit, "source_revision": revision, "lua_sha256": _hash(original),
             "sequences": [events[i]["sequence"] for i in
                           (before_index, index, returned_index, after_index)]}
    return proof, before, after


def build_recovery(memory, run_dir: Path) -> dict:
    """Build an opt-in reference without modifying memory or the game."""
    return _historical(memory, Path(run_dir).resolve())[0]


def check_recovery(memory, snapshot) -> dict | None:
    """Fail closed on any historical, checkpoint, or current-world mismatch."""
    reference = memory.transfer_recovery
    if not isinstance(reference, dict):
        return None
    try:
        proof, before, after = _historical(memory, Path(reference["run_dir"]))
        _require(proof == reference, "reference or checkpoint changed")
        current = asdict(snapshot)
        factory = before["factory"]
        machine = factory["entities"]["recipe:iron-plate"]
        live = _identity(current, memory.session_id, factory["craft_job_actor"], machine,
                         before["inventory"], factory["receipts"], factory["craft_job"], exhausted=True)
        _require(current["tick"] >= after["tick"], "current observation is stale")
        consumed = live["products_finished"] - machine["products_finished"]
        # Factorio has already consumed the ore for the in-progress smelt.
        # The initial five stored ore plus that one smelt can yield six plates.
        working = live["crafting"] is True and 0 < live["crafting_progress"] < 1
        idle = live["crafting"] is False and live["crafting_progress"] == 0
        remaining = live["input"].get("iron-ore", 0)
        _require(type(consumed) is int and 1 <= consumed <= 6
                 and type(remaining) is int and remaining >= 0
                 and set(live["input"]) <= {"iron-ore"} and (working or idle)
                 and consumed + remaining + int(working) == 6,
                 "current target violates conservation")
        return {"attempt_id": memory.attempt["id"], "events_sha256": proof["events_sha256"],
                "sequences": proof["sequences"], "transferred_quantity": 0}
    except (OSError, ValueError, KeyError, TypeError, AttributeError, StopIteration,
            subprocess.SubprocessError):
        return None
