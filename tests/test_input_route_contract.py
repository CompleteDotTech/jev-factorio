from copy import deepcopy

import pytest

from jev_factorio import input_routes as routes
from input_routes_fixtures import SOURCE, LAYOUT, fixture, row, parameters, build, full, commission


def test_full_kit_and_science_reserve_are_required():
    state = fixture()
    p = parameters(state)
    assert routes.allowed(p, state)
    assert routes.remaining(row(state), 20) == {"burner-inserter": 1, "transport-belt": 23, "burner-mining-drill": 1}
    state.inventory["transport-belt"] -= 1
    assert not routes.allowed(p, state)


def test_paid_component_is_not_flow():
    state = fixture()
    p = parameters(state)
    build(state, p)
    assert routes.component_complete(p, state)
    assert not routes.allowed(p, state)
    assert not routes.flow_complete(SOURCE, LAYOUT, state)
    p["receipt"] = "other"
    assert not routes.component_complete(p, state)


def test_all_steps_retain_reserve_and_verify_exact_receipts():
    state = fixture()
    for _ in row(state)["steps"]:
        p = parameters(state)
        assert routes.allowed(p, state)
        build(state, p)
        assert routes.component_complete(p, state)
    assert state.inventory["transport-belt"] == 20
    assert not routes.flow_complete(SOURCE, LAYOUT, state)
    commission(state)
    assert routes.flow_complete(SOURCE, LAYOUT, state)


@pytest.mark.parametrize("field,value", [
    ("reserve_belts", True), ("reserve_belts", -1), ("reserve_belts", 201), ("reserve_belts", 1.5),
    ("source", "recipe:steel-plate"), ("receipt", ""), ("receipt", "x"*129),
    ("part", "belt:0"), ("part", "belt:65"), ("part", "belt:01"), ("part", {}), ("layout", None),
])
def test_command_bounds(field, value):
    p = parameters(fixture()); p[field] = value
    with pytest.raises(ValueError): routes.validate(p)


@pytest.mark.parametrize("field,value", [("protocol", True), ("protocol", 2), ("tick", 299),
                                          ("session_id", "other"), ("sources", [])])
def test_stale_or_missing_envelope(field, value):
    state = fixture(); state.factory["input_routes"][field] = value
    with pytest.raises(ValueError): routes.sources(state)


@pytest.mark.parametrize("mutation", [
    lambda r: r.update(state="ready"), lambda r: r.update(source_unit=True),
    lambda r: r.update(topology=1), lambda r: r.update(steps=[]), lambda r: r.update(ore="copper-ore"),
    lambda r: r["steps"][1].update(direction=2),
    lambda r: r["steps"][1]["position"].update(x=float("nan")),
    lambda r: r["steps"][1]["position"].update(x=1.1),
    lambda r: r["steps"][1].update(position=r["steps"][0]["position"]),
    lambda r: r["steps"].reverse(), lambda r: r.update(parts={"drill": {}}),
])
def test_malformed_geometry_or_phase_is_not_authority(mutation):
    state = fixture(); mutation(row(state))
    with pytest.raises(ValueError): routes.sources(state)


@pytest.mark.parametrize("field,value", [("paid", True), ("paid", 0), ("unit_number", True), ("receipt", ""), ("role", "")])
def test_malformed_receipt_is_rejected(field, value):
    state = fixture(); p = parameters(state); build(state, p)
    row(state)["parts"][p["part"]][field] = value
    assert not routes.component_complete(p, state)


@pytest.mark.parametrize("field,value", [("received", 2), ("new_plates", 2), ("mined", 0), ("delivered", 0),
                                          ("positive_samples", True), ("conservation", 1), ("last_tick", 400),
                                          ("first_tick", 179), ("source_unit", True), ("layout", "other")])
def test_insufficient_or_malformed_flow_is_not_completion(field, value):
    state = full(fixture()); commission(state); row(state)["flow"][field] = value
    assert not routes.flow_complete(SOURCE, LAYOUT, state)


def test_native_identity_change_rejects_component_and_flow():
    state = full(fixture()); commission(state)
    state.factory["entities"]["input:belt:2"]["unit_number"] = 999
    assert not routes.current(row(state), state)
    assert not routes.flow_complete(SOURCE, LAYOUT, state)


def test_manual_ore_and_evidence_manipulation_are_blocked_but_fuel_is_allowed():
    state = full(fixture())
    assert not routes.permits("factory_insert", {"role": SOURCE, "item": "iron-ore"}, state)
    assert not routes.permits("factory_gather", {"resource": "iron-ore"}, state)
    assert not routes.permits("factory_insert", {"role": "input:belt:1", "item": "iron-ore"}, state)
    assert not routes.permits("factory_extract", {"role": "out:chest"}, state)
    assert routes.permits("factory_insert", {"role": SOURCE, "item": "coal"}, state)
    assert routes.permits("factory_insert", {"role": "input:drill", "item": "coal"}, state)
    commission(state)
    assert routes.permits("factory_extract", {"role": "out:chest"}, state)


def test_contract_helpers_do_not_mutate_evidence():
    state = fixture(); original = deepcopy(state)
    routes.sources(state); routes.allowed(parameters(state), state); routes.remaining(row(state), 20)
    assert state == original
