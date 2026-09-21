"""Offline simulated world. Proves the loop end-to-end with no game and no key."""
from __future__ import annotations

from uuid import uuid4

from ..state import GameSnapshot


class MockBackend:
    """Tiny scripted world: a coal patch 12 east, an iron patch 25 east.

    Coal depletes after 3 mines, forcing the agent to move on to iron and
    place the drill - a full bootstrap arc in a handful of ticks.
    """

    def __init__(self) -> None:
        self.session_id = "mock:" + uuid4().hex
        self.fuel = 0
        self.output = 0
        self.tick = 0
        self.pos = (0.0, 0.0)
        self.inv = {"burner-mining-drill": 1, "wooden-chest": 1}
        self.entities: list[str] = []
        self.at_resource: str | None = None
        self.coal_left = 3
        self.known = {"coal": 12.0, "iron-ore": 25.0}

    def observe(self) -> GameSnapshot:
        near = dict(self.known)
        if self.at_resource:
            near = {k: (0.0 if k == self.at_resource else v)
                    for k, v in near.items()}
        if self.coal_left == 0:
            near.pop("coal", None)
        return GameSnapshot(tick=self.tick, player_position=self.pos,
                            inventory=dict(self.inv), nearby_resources=near,
                            placed_entities=list(self.entities),
                            session_id=self.session_id, world_kind="mock",
                            drill_fuel=self.fuel, iron_ore_collected=self.output,
                            drill_output_connected=("wooden-chest" in self.entities
                                                    and any(e.startswith("burner-mining-drill")
                                                            for e in self.entities)),
                            drill_status="working" if self.fuel else "")

    def act(self, action: str) -> str:
        self.tick += 1
        if self.fuel > 0:
            self.fuel -= 1
            self.output += 2
        if action == "walk_to_coal" and "coal" in self.known and self.coal_left:
            self.at_resource = "coal"
            return "walked to coal patch"
        if action == "walk_to_iron":
            self.at_resource = "iron-ore"
            return "walked to iron patch"
        if action == "mine_coal" and self.at_resource == "coal" and self.coal_left:
            self.coal_left -= 1
            self.inv["coal"] = self.inv.get("coal", 0) + 5
            extra = " (patch depleted)" if self.coal_left == 0 else ""
            return f"mined 5 coal (have {self.inv['coal']}){extra}"
        if action == "mine_iron" and self.at_resource == "iron-ore":
            self.inv["iron-ore"] = self.inv.get("iron-ore", 0) + 5
            return f"mined 5 iron ore (have {self.inv['iron-ore']})"
        if action == "place_burner_drill" and self.at_resource == "iron-ore":
            if self.inv.get("burner-mining-drill", 0) > 0 and self.inv.get("wooden-chest", 0) > 0:
                self.inv["burner-mining-drill"] -= 1
                self.inv["wooden-chest"] -= 1
                self.entities.append("wooden-chest")
                self.entities.append(f"burner-mining-drill@{self.at_resource}")
                return f"placed burner drill on {self.at_resource}"
            return "no drill in inventory"
        if action == "fuel_drill":
            if self.inv.get("coal", 0) > 0 and self.entities:
                amount = min(5, self.inv["coal"])
                self.inv["coal"] -= amount
                self.fuel += amount
                return f"fueled drill with {amount} coal"
            return "nothing to fuel"
        return f"({action}: nothing to do)"
