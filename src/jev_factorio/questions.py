"""Build typed Jev questions from a GameSnapshot.

One HTTP call per decision tick carries ALL questions (speculative fan-out:
https://docs.typesafe.ai/patterns/fan-out.md). Jev bills input tokens only,
so extra questions are nearly free.
"""
from __future__ import annotations

from .state import GameSnapshot

# Every action the MVP can execute. Values are rubric text Jev reads.
ACTION_RUBRIC = {
    "walk_to_iron": "Move toward the nearest iron-ore patch",
    "walk_to_coal": "Move toward the nearest coal patch",
    "mine_iron": "Hand-mine iron ore at the current position",
    "mine_coal": "Hand-mine coal at the current position",
    "place_burner_drill": "Place a burner mining drill on the resource under the player",
    "fuel_drill": "Insert coal into a placed burner drill that is low on fuel",
    "craft_stone_furnace": "Craft a stone furnace from hand-mined stone/iron",
    "idle": "Do nothing this tick; the current plan is progressing",
}

GOAL_RUBRIC = {
    "bootstrap_mining": "Get the first burner mining drill producing iron ore",
    "stockpile_fuel": "Build a small coal buffer before placing machinery",
    "automate_smelting": "Move from hand work to furnace automation",
    "hold": "No goal change; keep executing the current plan",
}


def build_questions(snapshot: GameSnapshot) -> dict:
    """Typed questions for one decision tick. Keys are caller-chosen ids."""
    candidates = _candidate_actions(snapshot)
    return {
        "goal": {
            "type": "choice",
            "instructions": "Given this Factorio game state, what should the agent's current goal be?",
            "criteria": GOAL_RUBRIC,
        },
        "next_action": {
            "type": "choice",
            "instructions": (
                "Bootstrap iron mining: first gather at least 5 coal, then move to iron, "
                "place the burner drill, and fuel it. Do not return to coal when enough "
                "fuel is already in inventory. Placement also adds an output chest. "
                "Choose idle once the drill is working and iron is accumulating. "
                "Only choose an action from the supplied options."
            ),
            "criteria": {k: ACTION_RUBRIC[k] for k in candidates},
        },
        "is_stuck": {
            "type": "noul",
            "instructions": "Is the agent stuck (no progress possible with the current plan)?",
            "criteria": {"true": "No progress possible", "false": "Progress is possible"},
        },
        "urgency": {
            "type": "score",
            "instructions": "How urgently does the agent need to change what it is doing?",
            "criteria": ["fine as-is", "adjust soon", "change immediately"],
        },
    }


def _candidate_actions(snapshot: GameSnapshot) -> list[str]:
    """Hard game rules filter the option set BEFORE Jev sees it.

    Jev always picks a winner, even when nothing fits - so never offer an
    action the game state makes impossible.
    """
    cands = ["idle"]
    inv = snapshot.inventory
    near = snapshot.nearby_resources
    here = {name for name, dist in near.items() if dist <= 0.5}
    if "iron-ore" in near and "iron-ore" not in here:
        cands.append("walk_to_iron")
    if "coal" in near and "coal" not in here:
        cands.append("walk_to_coal")
    if "iron-ore" in here:
        cands.append("mine_iron")
    if "coal" in here:
        cands.append("mine_coal")
    if inv.get("burner-mining-drill", 0) > 0 and "iron-ore" in here:
        cands.append("place_burner_drill")
    if any(e.startswith("burner-mining-drill") for e in snapshot.placed_entities) \
            and inv.get("coal", 0) > 0:
        cands.append("fuel_drill")
    if inv.get("stone", 0) >= 5:
        cands.append("craft_stone_furnace")
    return cands
