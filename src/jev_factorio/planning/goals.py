"""Goal dependencies are code; completion requires authoritative observations."""
from __future__ import annotations

from dataclasses import dataclass

from ..state import GameSnapshot


@dataclass(frozen=True)
class Goal:
    id: str
    description: str
    prerequisites: tuple[str, ...] = ()


GOALS = {
    "stockpile_fuel": Goal("stockpile_fuel", "Gather a five-coal construction buffer"),
    "bootstrap_mining": Goal(
        "bootstrap_mining", "Observe a working iron drill and at least five collected ore",
        ("stockpile_fuel",),
    ),
    # A terminal objective, NOT a fabricated technology/recipe graph. Further
    # production skills and version-specific victory telemetry must be supplied.
    "rocket_launch": Goal(
        "rocket_launch", "Verify a native base-game rocket-launch completion event",
        ("bootstrap_mining",),
    ),
}


def goal_order(target: str, goals: dict[str, Goal] | None = None) -> list[str]:
    """Return prerequisites before dependents; reject unknown nodes and cycles."""
    goals = GOALS if goals is None else goals
    order: list[str] = []
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(key: str) -> None:
        if key not in goals:
            raise ValueError(f"Unknown goal: {key}")
        if key in visiting:
            raise ValueError(f"Cyclic goal dependency at {key}")
        if key in visited:
            return
        visiting.add(key)
        for parent in goals[key].prerequisites:
            visit(parent)
        visiting.remove(key)
        visited.add(key)
        order.append(key)

    visit(target)
    return order


def completed(goal: str, snapshot: GameSnapshot) -> bool:
    """Never accept a model answer or an action's success text as evidence."""
    if goal == "stockpile_fuel":
        return snapshot.inventory.get("coal", 0) >= 5
    if goal == "bootstrap_mining":
        return (snapshot.drill_output_connected is True
                and snapshot.drill_status == "working"
                and any(e.startswith("burner-mining-drill") for e in snapshot.placed_entities)
                and snapshot.iron_ore_collected >= 5)
    if goal == "rocket_launch":
        return (snapshot.victory is True
                and snapshot.victory_source == "native:base-game-rocket-launch")
    raise ValueError(f"No verifier for goal: {goal}")
