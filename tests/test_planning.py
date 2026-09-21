import json

import pytest

from jev_factorio.memory import CampaignMemory
from jev_factorio.planning.goals import Goal, goal_order
from jev_factorio.planning.materials import Recipe, requirements


def test_goal_order_is_dependency_first_and_deduplicated():
    goals = {"a": Goal("a", "a"), "b": Goal("b", "b", ("a",)),
             "c": Goal("c", "c", ("a", "b"))}
    assert goal_order("c", goals) == ["a", "b", "c"]


def test_goal_cycle_and_unknown_goal_are_rejected():
    with pytest.raises(ValueError, match="Cyclic"):
        goal_order("a", {"a": Goal("a", "a", ("a",))})
    with pytest.raises(ValueError, match="Unknown"):
        goal_order("not-a-goal")


def test_materials_account_for_inventory_batching_and_reservations():
    recipes = [Recipe("plate", {"ore": 1}, {"plate": 1}),
               Recipe("gear", {"plate": 2}, {"gear": 1})]
    plan = requirements({"gear": 3}, {"plate": 3}, recipes, reserved={"plate": 1})
    assert plan.batches == {"plate": 4, "gear": 3}
    assert plan.shortages == {"ore": 4}
    assert plan.remaining["plate"] == 0


def test_co_products_are_not_double_counted():
    plan = requirements({"a": 2, "b": 3}, {},
                        [Recipe("multi", {"raw": 1}, {"a": 2, "b": 3})])
    assert plan.batches == {"multi": 1}
    assert plan.shortages == {"raw": 1}
    assert plan.remaining == {"a": 0, "raw": 0, "b": 0}


def test_alternative_recipes_need_explicit_selection():
    recipes = [Recipe("a", {"x": 1}, {"plate": 1}), Recipe("b", {"y": 1}, {"plate": 1})]
    with pytest.raises(ValueError, match="explicitly"):
        requirements({"plate": 1}, {}, recipes)
    assert requirements({"plate": 1}, {}, recipes, selected={"plate": "b"}).shortages == {"y": 1}
    with pytest.raises(ValueError, match="unavailable"):
        requirements({"plate": 1}, {}, recipes, selected={"plate": "missing"})


def test_disabled_recipes_and_cycles_are_not_assumed_away():
    result = requirements({"plate": 1}, {}, [Recipe("locked", {"ore": 1}, {"plate": 1}, False)])
    assert result.shortages == {"plate": 1}
    with pytest.raises(ValueError, match="Cyclic"):
        requirements({"a": 1}, {}, [Recipe("a", {"b": 1}, {"a": 1}),
                                     Recipe("b", {"a": 1}, {"b": 1})])


@pytest.mark.parametrize("value", [-1, float("nan"), float("inf"), True, "5"])
def test_materials_reject_invalid_quantities(value):
    with pytest.raises(ValueError):
        requirements({"ore": value}, {}, [])


def test_material_expansion_is_bounded():
    with pytest.raises(ValueError, match="budget"):
        requirements({"a": 1}, {}, [Recipe("a", {"b": 1}, {"a": 1})], max_expansions=1)


def test_duplicate_recipes_and_overreservation_fail():
    r = Recipe("a", {}, {"a": 1})
    with pytest.raises(ValueError, match="Duplicate"):
        requirements({"a": 1}, {}, [r, r])
    with pytest.raises(ValueError, match="exceeds"):
        requirements({}, {}, [], reserved={"ore": 1})


def test_reservations_prevent_double_spending_and_release():
    memory = CampaignMemory("session", "bootstrap_mining")
    memory.reserve("a", {"coal": 4}, {"coal": 5})
    with pytest.raises(ValueError, match="unreserved"):
        memory.reserve("b", {"coal": 2}, {"coal": 5})
    memory.reserve("a", {"coal": 5}, {"coal": 5})
    memory.release("a")
    memory.reserve("b", {"coal": 5}, {"coal": 5})


def test_checkpoint_round_trip_and_session_target_binding(tmp_path):
    path = tmp_path / "state.json"
    memory = CampaignMemory("s", "bootstrap_mining")
    memory.event("test", tick=1)
    memory.save(path)
    assert CampaignMemory.load(path, "s", "bootstrap_mining") == memory
    for session, target in [("other", "bootstrap_mining"), ("s", "rocket_launch")]:
        with pytest.raises(ValueError, match="mismatch"):
            CampaignMemory.load(path, session, target)
    assert list(tmp_path.iterdir()) == [path]


def test_corrupt_checkpoint_not_reset(tmp_path):
    path = tmp_path / "state.json"
    path.write_text('{"version":')
    with pytest.raises(ValueError, match="checkpoint"):
        CampaignMemory.load(path, "s", "bootstrap_mining")
    assert path.read_text() == '{"version":'


def test_history_is_bounded():
    memory = CampaignMemory("s", "bootstrap_mining")
    for i in range(200):
        memory.event("test", tick=i)
    assert len(memory.history) == 64
    assert memory.history[-1]["tick"] == 199


@pytest.mark.parametrize("field,value", [("version", 2), ("step_index", -1),
                                         ("completed_goals", {"rocket_launch": 99}),
                                         ("history", ["not-an-event"]),
                                         ("failures", {"plan": -1})])
def test_checkpoint_schema_validation(tmp_path, field, value):
    from dataclasses import asdict

    data = asdict(CampaignMemory("s", "bootstrap_mining"))
    data[field] = value
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError):
        CampaignMemory.load(path, "s", "bootstrap_mining")
