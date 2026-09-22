"""Pure telemetry-contract tests; no engine or provider is initialized."""
from copy import deepcopy
from types import SimpleNamespace

import pytest

from jev_factorio.output_buffers import (
    allowed, component_complete, flow_complete, permits, potential, sources, validate,
)


def scenario():
    row = {"source": "recipe:iron-plate", "source_unit": 17, "layout": "output:17:1:0:4",
           "item": "iron-plate", "state": "proposed", "chest_role": "output-chest:17",
           "parts": {}, "topology": False, "held": 0}
    state = SimpleNamespace(session_id="test", tick=200, inventory={"wooden-chest": 1},
        factory={"player_connected": True, "player_bound": True, "crafting_queue": 0,
                 "entities": {row["source"]: {"name": "stone-furnace", "unit_number": 17,
                     "input": {"iron-ore": 9}, "output": {"iron-plate": 2}, "crafting": True}},
                 "output_buffers": {"protocol": 1, "session_id": "test", "tick": 200,
                                    "sources": {row["source"]: row}}})
    parameters = {"source": row["source"], "layout": row["layout"], "part": "chest", "receipt": "r1"}
    return state, row, parameters


def installed():
    state, row, parameters = scenario()
    for part, role, name, unit in (("chest", "output-chest:17", "wooden-chest", 18),
                                    ("inserter", "output-arm:17", "burner-inserter", 19)):
        row["parts"][part] = {"role": role, "unit_number": unit, "receipt": "r1", "paid": 1}
        state.factory["entities"][role] = {"unit_number": unit, "name": name,
                                             "output": {"iron-plate": 3}}
    row["state"], row["topology"] = "ready", True
    row["flow"] = {"layout": row["layout"], "source_unit": 17, "first_tick": 30,
                   "last_tick": 180, "positive_samples": 3, "received": 3, "conservation": True}
    return state, row, parameters


def test_empty_layout_can_build_only_paid_first_component():
    state, row, parameters = scenario()
    assert allowed(parameters, state)
    assert not component_complete(parameters, state)
    state.inventory.clear()
    assert not allowed(parameters, state)
    state.inventory["burner-inserter"] = 1
    assert not allowed({**parameters, "part": "inserter"}, state)


@pytest.mark.parametrize("change", [
    {"part": "belt"}, {"part": True}, {"receipt": ""}, {"source": []},
    {"layout": "x" * 129}, {"quantity": 1},
])
def test_invalid_commands_rejected(change):
    _, _, p = scenario()
    with pytest.raises(ValueError):
        validate({**p, **change})


@pytest.mark.parametrize("field,value", [
    ("protocol", True), ("protocol", 2), ("session_id", "other"),
    ("tick", 199), ("tick", True), ("sources", []),
])
def test_stale_or_wrong_session_evidence_is_not_authority(field, value):
    state, _, p = scenario()
    state.factory["output_buffers"][field] = value
    with pytest.raises(ValueError):
        sources(state)
    assert not allowed(p, state)
    assert not component_complete(p, state)


def test_replacement_source_cannot_reuse_layout_or_receipt():
    state, row, p = installed()
    state.factory["entities"][row["source"]]["unit_number"] = 999
    assert not allowed(p, state)
    assert not component_complete(p, state)
    assert not flow_complete(row["source"], row["layout"], state)


def test_paid_build_and_flow_are_different_facts():
    state, row, p = installed()
    row["flow"] = {}
    assert component_complete(p, state)
    assert not allowed(p, state)
    assert not flow_complete(row["source"], row["layout"], state)


@pytest.mark.parametrize("field,value", [
    ("receipt", "unrelated"), ("paid", 0), ("paid", True), ("unit_number", 99),
])
def test_component_receipt_must_match_paid_live_identity(field, value):
    state, row, p = installed()
    row["parts"]["chest"][field] = value
    assert not component_complete(p, state)


@pytest.mark.parametrize("change", [
    {"first_tick": 100}, {"last_tick": 201}, {"positive_samples": 2}, {"received": 2},
    {"conservation": False}, {"conservation": 1}, {"layout": "other"}, {"source_unit": 99},
    {"last_tick": float("nan")}, {"received": True}, {"first_tick": -1},
])
def test_commissioning_requires_bounded_conservation_window(change):
    state, row, _ = installed()
    row["flow"].update(change)
    assert not flow_complete(row["source"], row["layout"], state)


def test_commissioning_and_current_topology_both_required():
    state, row, _ = installed()
    assert flow_complete(row["source"], row["layout"], state)
    row["topology"] = False
    assert not flow_complete(row["source"], row["layout"], state)


def test_no_actor_seeding_or_racing_and_no_premature_collection():
    state, row, _ = installed()
    assert not permits("factory_insert", {"role": row["chest_role"]}, state)
    assert not permits("factory_extract", {"role": row["source"]}, state)
    assert not permits("factory_configure", {"role": row["source"]}, state)
    assert permits("factory_insert", {"role": "output-arm:17", "item": "coal"}, state)
    assert permits("factory_extract", {"role": row["chest_role"]}, state)
    row["flow"] = {}
    assert not permits("factory_extract", {"role": row["chest_role"]}, state)


def test_forecast_is_derived_without_changing_observation():
    state, row, _ = installed()
    recipe = {"ingredients": [{"name": "iron-ore", "amount": 1, "type": "item"}],
              "products": [{"name": "iron-plate", "amount": 1}]}
    before = deepcopy(state)
    assert potential(row, state, recipe) == 15  # 3 chest + 2 furnace + 9 buffered + 1 in flight.
    assert vars(state) == vars(before)
