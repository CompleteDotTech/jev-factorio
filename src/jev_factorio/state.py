"""Game state snapshot and its compact rendering for Jev.

Jev sees a compact, structured `state` object (see
https://docs.typesafe.ai/concepts/state.md). Keep it small: only what the
current decision tier needs. Raw entity dumps belong to the backend, not
to the model.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict


@dataclass
class GameSnapshot:
    tick: int = 0
    player_position: tuple[float, float] = (0.0, 0.0)
    inventory: dict[str, int] = field(default_factory=dict)
    nearby_resources: dict[str, float] = field(default_factory=dict)  # name -> distance
    placed_entities: list[str] = field(default_factory=list)
    power_satisfaction: float = 1.0   # 1.0 = full, burner stage has no electric network
    craft_queue: list[str] = field(default_factory=list)
    alerts: list[str] = field(default_factory=list)  # e.g. "drill out of fuel"
    drill_status: str = ""
    drill_fuel: int = 0
    iron_ore_collected: int = 0

    def for_jev(self) -> dict:
        """Compact structured state sent to Jev."""
        d = asdict(self)
        d["player_position"] = [round(self.player_position[0], 1),
                                round(self.player_position[1], 1)]
        return d
