from jev_factorio.backends.mock import MockBackend
from jev_factorio.jev_client import MockJevClient
from jev_factorio.loop import AgentLoop, fallback_policy
from jev_factorio.questions import build_questions
from jev_factorio.state import GameSnapshot


def test_candidates_exclude_impossible_actions():
    s = GameSnapshot(inventory={}, nearby_resources={})
    q = build_questions(s)
    assert set(q["next_action"]["criteria"]) == {"idle"}


def test_mock_loop_runs_end_to_end():
    loop = AgentLoop(MockBackend(), jev=MockJevClient(), tick_seconds=0)
    rec = loop.step()
    assert rec["action"] and rec["outcome"]


def test_fallback_places_drill_plan():
    s = GameSnapshot(inventory={"burner-mining-drill": 1},
                     nearby_resources={"coal": 12.0})
    assert fallback_policy(s) == "walk_to_coal"


def test_fallback_keeps_coal_buffer_and_progresses_to_iron():
    snapshot = GameSnapshot(
        inventory={"burner-mining-drill": 1, "coal": 5},
        nearby_resources={"coal": 0.0, "iron-ore": 25.0},
    )
    assert fallback_policy(snapshot) == "walk_to_iron"
    snapshot.nearby_resources = {"coal": 12.0, "iron-ore": 0.0}
    assert fallback_policy(snapshot) == "place_burner_drill"


def test_fallback_fuels_then_waits_for_production():
    snapshot = GameSnapshot(
        inventory={"coal": 5},
        placed_entities=["burner-mining-drill"],
    )
    assert fallback_policy(snapshot) == "fuel_drill"
    snapshot.drill_status = "working"
    snapshot.drill_fuel = 4
    assert fallback_policy(snapshot) == "idle"


def test_candidates_do_not_place_iron_drill_on_coal_or_repeat_walk():
    snapshot = GameSnapshot(
        inventory={"burner-mining-drill": 1},
        nearby_resources={"coal": 0.0, "iron-ore": 25.0},
    )
    candidates = build_questions(snapshot)["next_action"]["criteria"]
    assert "place_burner_drill" not in candidates
    assert "walk_to_coal" not in candidates
    assert "mine_coal" in candidates


def test_unavailable_model_action_uses_fallback_and_logs_state(tmp_path):
    import json

    class InvalidChoiceClient(MockJevClient):
        def evaluate(self, state, questions):
            answers = super().evaluate(state, questions)
            answers["next_action"]["choice"] = "place_burner_drill"
            answers["next_action"]["confidence"] = 1.0
            return answers

    log_file = tmp_path / "decisions.jsonl"
    loop = AgentLoop(
        MockBackend(), jev=InvalidChoiceClient(), tick_seconds=0,
        log_file=str(log_file),
    )
    record = loop.step()
    assert record["source"] == "fallback"
    assert record["action"] == "walk_to_coal"
    logged = json.loads(log_file.read_text())
    assert logged["state"]["nearby_resources"]["coal"] == 12.0
    assert logged["after_state"]["nearby_resources"]["coal"] == 0.0
