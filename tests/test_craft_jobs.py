"""Synthetic evidence, not native gameplay or throughput measurements."""
from copy import deepcopy
from types import SimpleNamespace

import pytest

from jev_factorio.craft_jobs import CraftJob, InvalidCraftEvidence, craft_complete, receipt_for


def parameters():
    return {"recipe": "science", "batches": 10, "receipt": "request-1"}


def state():
    actor = {"session_id": "session-1", "player_index": 1, "unit_number": 9,
             "surface_index": 1, "force_index": 1}
    receipt = {**actor, "id": "request-1", "recipe": "science", "requested": 10,
               "accepted": 10, "finished": 2, "started_tick": 100, "last_progress_tick": 120,
               "inputs": {"iron-plate": 10}, "outputs": {"science": 10},
               "baseline": {"science": 3}, "status": "running", "queue_valid": True, "paid": True}
    return SimpleNamespace(session_id="session-1", tick=130, inventory={"science": 5},
                           factory={"craft_jobs_protocol": 1, "craft_job_actor": actor,
                                    "craft_job": receipt, "player_connected": True,
                                    "player_bound": True, "crafting_queue": 1})


def admitted():
    return CraftJob(parameters(), "craft-science", "rocket_launch", "session-1",
                    {"player_index": 1, "unit_number": 9, "surface_index": 1, "force_index": 1},
                    {"iron-plate": 10}, {"science": 10}, {"science": 3}, 100, 1000,
                    finished=2, last_progress_tick=120)


def completed(s):
    s.factory["craft_job"].update(status="completed", finished=10, last_progress_tick=200,
                                 completed_tick=200)
    s.factory["crafting_queue"] = 0
    s.tick = 210
    s.inventory["science"] = 13
    return s


def test_receipt_and_output_are_both_required():
    s = state()
    assert not craft_complete(parameters(), s)
    s.inventory["science"] = 100
    assert not craft_complete(parameters(), s)
    completed(s)
    assert craft_complete(parameters(), s)
    s.inventory["science"] -= 1
    assert not craft_complete(parameters(), s)


@pytest.mark.parametrize("field,value", [
    ("id", "another-request"), ("recipe", "other"), ("session_id", "other"),
    ("player_index", 2), ("unit_number", 10), ("surface_index", 2), ("force_index", 2),
    ("accepted", 9), ("accepted", True), ("requested", 0), ("requested", True),
    ("finished", -1), ("finished", 11), ("finished", True), ("finished", 2.5),
    ("started_tick", -1), ("started_tick", 140), ("last_progress_tick", 99),
    ("last_progress_tick", 131), ("last_progress_tick", float("nan")),
    ("status", "cancelled"), ("status", "invalid"), ("status", "completed"),
    ("paid", False), ("paid", 1), ("queue_valid", False), ("queue_valid", 1), ("outputs", {}), ("outputs", {"science": 11}),
    ("outputs", {"science": True}), ("baseline", {}), ("inputs", {}),
    ("inputs", {"science": 10}), ("outputs", {"science": 10, "iron-plate": 10}),
])
def test_rejects_mismatched_or_malformed_native_evidence(field, value):
    s = state()
    s.factory["craft_job"][field] = value
    with pytest.raises(InvalidCraftEvidence):
        receipt_for(parameters(), s)
    assert not craft_complete(parameters(), s)


@pytest.mark.parametrize("field", ["craft_job", "craft_job_actor", "craft_jobs_protocol",
                                    "player_connected", "player_bound"])
def test_missing_evidence_cannot_authorize_completion(field):
    s = completed(state())
    s.factory.pop(field)
    assert not craft_complete(parameters(), s)


def test_progress_monotonicity_and_preserved_deadline():
    job, s = admitted(), state()
    s.factory["craft_job"].update(finished=3, last_progress_tick=129)
    s.inventory["science"] = 6
    assert job.observe(s) is False
    assert job.finished == 3 and job.deadline_tick == 1000
    s.factory["craft_job"]["finished"] = 2
    with pytest.raises(InvalidCraftEvidence, match="regressed"):
        job.observe(s)


def test_late_observation_accepts_timely_native_completion():
    s = completed(state())
    s.tick = 2000
    assert admitted().observe(s)
    s.factory["craft_job"].update(completed_tick=1001, last_progress_tick=1001)
    with pytest.raises(InvalidCraftEvidence, match="late"):
        admitted().observe(s)


def test_timeout_and_external_output_consumption_fail_closed():
    s = state()
    s.tick = 1000
    with pytest.raises(InvalidCraftEvidence, match="deadline"):
        admitted().observe(s)
    s.tick = 130
    s.inventory["science"] = 4
    with pytest.raises(InvalidCraftEvidence, match="consumed"):
        admitted().observe(s)


@pytest.mark.parametrize("key,value", [("baseline", {"science": 0}), ("outputs", {"science": 20}),
                                        ("inputs", {"iron-plate": 20}), ("started_tick", 99)])
def test_valid_but_changed_receipt_is_not_the_same_job(key, value):
    s = state()
    s.factory["craft_job"][key] = value
    with pytest.raises(InvalidCraftEvidence, match="changed"):
        admitted().observe(s)


def test_job_checkpoint_roundtrip_is_detached():
    job = admitted()
    restored = CraftJob.from_dict(job.to_dict())
    assert restored == job
    restored.inputs["iron-plate"] = 999
    assert job.inputs == {"iron-plate": 10}


@pytest.mark.parametrize("key,value", [("parameters", {}), ("deadline_tick", 100),
                                        ("finished", 10), ("actor", {}), ("session_id", ""),
                                        ("failed", []), ("background_unrecognized", True)])
def test_invalid_checkpoint_is_rejected(key, value):
    data = admitted().to_dict()
    data[key] = value
    with pytest.raises(InvalidCraftEvidence):
        CraftJob.from_dict(data)


def test_locks_only_release_after_verified_completion():
    job = admitted()
    def step(action, **values):
        return SimpleNamespace(action=action, costs=values.pop("costs", {}), parameters=values)
    assert job.permits(step("factory_gather", resource="iron-ore"))
    assert job.permits(step("factory_insert", item="coal", costs={"coal": 4}))
    assert job.permits(step("factory_extract", item="iron-plate"))
    assert not job.permits(step("factory_insert", item="science", costs={"science": 1}))
    assert not job.permits(step("factory_extract", item="science"))
    assert not job.permits(step("factory_craft"))
    assert not job.permits(step("factory_craft_job"))
    assert not job.permits(step("factory_place"))
    job.failed = "checkpoint uncertain"
    assert not job.permits(step("factory_gather", resource="iron-ore"))


def test_admission_requires_returned_dispatch_and_matching_paid_inputs():
    s = state()
    step = SimpleNamespace(action="factory_craft_job", parameters=parameters(),
                           costs={"iron-plate": 10}, timeout_ticks=900)
    plan = SimpleNamespace(id="craft-science", goal="rocket_launch", steps=[step])
    pending = {"action": "factory_craft_job", "dispatch": "returned", "started_tick": 100}
    catalog = SimpleNamespace(recipes={"science": {
        "ingredients": [{"name": "iron-plate", "amount": 1, "type": "item"}],
        "products": [{"name": "science", "amount": 1, "type": "item"}],
    }})
    assert CraftJob.admit(plan, pending, s, catalog) == admitted()
    for dispatch in ("prepared", "ambiguous"):
        with pytest.raises(InvalidCraftEvidence):
            CraftJob.admit(plan, {**pending, "dispatch": dispatch}, s, catalog)
    step.costs = {"iron-plate": 9}
    with pytest.raises(InvalidCraftEvidence, match="committed"):
        CraftJob.admit(plan, pending, s, catalog)


def test_boolean_protocol_is_not_version_one():
    s = completed(state())
    s.factory["craft_jobs_protocol"] = True
    assert not craft_complete(parameters(), s)
