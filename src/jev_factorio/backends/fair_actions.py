"""Player-controlled actions without teleporting or synthetic harvests."""
from __future__ import annotations

import json
import math
import time
from importlib.resources import files
from types import SimpleNamespace
from typing import Any


class FairActions:
    def __init__(self, backend: Any) -> None:
        self.backend = backend
        self.command(files("jev_factorio").joinpath("lua/fair_actions.lua").read_text())
        self.call("bind")

    def command(self, script: str) -> str:
        result = self.backend._instance.rcon_client.send_command("/sc " + script) or ""
        if result.startswith("Cannot execute command."):
            raise RuntimeError(result)
        return result

    def call(self, function: str, *arguments: Any) -> dict:
        encoded = ", ".join(
            "helpers.json_to_table(" + json.dumps(json.dumps(value, allow_nan=False)) + ")"
            for value in arguments
        )
        return json.loads(self.command(
            f"rcon.print(helpers.table_to_json(storage.fair.{function}({encoded})))"
        ))

    @staticmethod
    def position(position: Any) -> dict:
        result = {"x": float(position.x), "y": float(position.y)}
        if not all(math.isfinite(value) for value in result.values()):
            raise ValueError("Position must be finite")
        return result

    def wait(self, timeout: float = 180) -> dict:
        deadline = time.monotonic() + timeout
        try:
            while time.monotonic() < deadline:
                state = self.call("observe")
                if state["status"] == "completed":
                    return state
                if state["status"] == "failed":
                    raise RuntimeError(state.get("error", "Native controls failed"))
                time.sleep(0.1)
            raise TimeoutError("Native action exceeded its bounded observation window")
        finally:
            self.command("storage.fair.stop()")

    def move_to(self, position: Any) -> Any:
        from fle.env import Position

        self.call("begin_move", self.position(position))
        state = self.wait()
        result = Position(**state["position"])
        self.backend._instance.namespace.player_location = result
        return result

    def approach(self, position: Any, name: str = "character") -> None:
        from fle.env import Position

        center = self.position(position)
        result = json.loads(self.command(
            "local player = storage.fair.actor(); local prototype = prototypes.entity["
            + json.dumps(name) + "]; local box = prototype.selection_box; "
            "local target = helpers.json_to_table(" + json.dumps(json.dumps(center)) + "); "
            "target.x = target.x + math.max(math.abs(box.left_top.x), "
            "math.abs(box.right_bottom.x)) + 1.5; "
            "local position = player.surface.find_non_colliding_position('character', target, 8, 0.25); "
            "assert(position, 'No collision-free approach'); rcon.print(helpers.table_to_json(position))"
        ))
        self.move_to(Position(**result))

    def harvest(self, resource: str, position: Any, quantity: int) -> int:
        from fle.env import Position

        if type(quantity) is not int or quantity <= 0:
            raise ValueError("Mining quantity must be positive")
        gained = 0
        for attempt in range(quantity):
            target = json.loads(self.command(
                "local player = storage.fair.actor(); local center = helpers.json_to_table("
                + json.dumps(json.dumps(self.position(position))) + "); "
                "local filter = {position=center, radius=32}; "
                + ("filter.type='tree'; " if resource == "wood" else
                   "filter.name=" + json.dumps(resource) + "; ")
                + "local entities = player.surface.find_entities_filtered(filter); "
                "table.sort(entities, function(left, right) return "
                "(left.position.x-player.position.x)^2+(left.position.y-player.position.y)^2 < "
                "(right.position.x-player.position.x)^2+(right.position.y-player.position.y)^2 end); "
                "assert(entities[1], 'No nearby mining target'); "
                "rcon.print(helpers.table_to_json(entities[1].position))"
            ))
            self.approach(Position(**target))
            self.call("begin_mine", target, resource, quantity - gained)
            try:
                result = self.wait()
            except RuntimeError:
                result = self.call("observe")
                if result.get("error") != "Resource depleted before requested amount" or result["gained"] <= 0:
                    raise
            gained += result["gained"]
            if gained >= quantity:
                return gained
        raise RuntimeError("Mining target budget exhausted")

    def place_entity(self, prototype: Any, position: Any, direction: Any,
                     exact: bool = False) -> Any:
        from fle.env import Position

        name = prototype.value[0]
        target = self.position(position)
        self.approach(position, name)
        if not exact:
            target = json.loads(self.command(
                "local player = storage.fair.actor(); local position = "
                "player.surface.find_non_colliding_position(" + json.dumps(name) + ", "
                "helpers.json_to_table(" + json.dumps(json.dumps(target)) + "), 8, 0.5); "
                "assert(position, 'No ordinary build site'); rcon.print(helpers.table_to_json(position))"
            ))
            self.approach(Position(**target), name)
        result = self.call("place", name, target, direction.value)
        return SimpleNamespace(
            name=result["name"], position=Position(**result["position"]),
            drop_position=Position(**result["drop_position"]) if result.get("drop_position") else None,
        )

    def insert_item(self, prototype: Any, entity: Any, quantity: int) -> int:
        self.approach(entity.position, entity.name)
        return self.call("insert", entity.name, self.position(entity.position),
                         prototype.value[0], quantity)["quantity"]

    def connect(self, source: Any, target: Any, prototype: Any, fluid: str = "") -> None:
        from fle.env import Direction, Position
        from ..planning.connections import shortest_pipe_path, select_pole_positions

        name = prototype.value[0]
        if name not in {"pipe", "small-electric-pole"}:
            raise ValueError("Unsupported fair connection type")
        start = self.position(getattr(source, "position", source))
        end = self.position(getattr(target, "position", target))
        left, right = math.floor(min(start["x"], end["x"])) - 8, math.ceil(max(start["x"], end["x"])) + 8
        top, bottom = math.floor(min(start["y"], end["y"])) - 8, math.ceil(max(start["y"], end["y"])) + 8
        if (right - left + 1) * (bottom - top + 1) > 16384:
            raise ValueError("Connection search exceeds bounded area")
        cells = json.loads(self.command(
            "local player = storage.fair.actor(); local result = {buildable={}, existing={}}; "
            f"for horizontal={left},{right} do for vertical={top},{bottom} do "
            "local position = {x=horizontal+0.5,y=vertical+0.5}; "
            "local entity = player.surface.find_entity(" + json.dumps(name) + ", position); "
            "if entity and entity.force == player.force then "
            "local contents = #entity.fluidbox > 0 and entity.fluidbox[1]; "
            "if not contents or contents.name == " + json.dumps(fluid) + " then "
            "table.insert(result.existing, position) end "
            "elseif player.surface.can_place_entity{name=" + json.dumps(name)
            + ", position=position, force=player.force, build_check_type=defines.build_check_type.manual} "
            "then table.insert(result.buildable, position) end end end; "
            "rcon.print(helpers.table_to_json(result))"
        ))
        buildable = {(point["x"], point["y"]) for point in cells["buildable"]}
        existing = {(point["x"], point["y"]) for point in cells["existing"]}
        origin, destination = (start["x"], start["y"]), (end["x"], end["y"])
        if name == "small-electric-pole":
            candidates = buildable | existing
            if not candidates:
                raise ValueError("No ordinary pole placement cells")
            origin = min(candidates, key=lambda point: math.dist(point, origin))
            destination = min(candidates, key=lambda point: math.dist(point, destination))
            if math.dist(origin, (start["x"], start["y"])) > 3.5 or math.dist(
                destination, (end["x"], end["y"])
            ) > 3.5:
                raise ValueError("No nearby ordinary pole placement")
        route = shortest_pipe_path(origin, destination, buildable, existing)
        if name == "small-electric-pole":
            route = select_pole_positions(route, max_wire_distance=6)
        required = sum(point not in existing for point in route)
        available = json.loads(self.command(
            "rcon.print(helpers.table_to_json({count=storage.fair.actor().get_item_count("
            + json.dumps(name) + ")}))"
        ))["count"]
        if available < required:
            raise ValueError(f"Fair connection needs {required} {name}, only {available} available")
        for horizontal, vertical in route:
            if (horizontal, vertical) not in existing:
                self.place_entity(prototype, Position(x=horizontal, y=vertical),
                                  direction=Direction.UP, exact=True)
