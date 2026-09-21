"""FLE adapter for a dedicated, explicitly marked Factorio agent world."""
from __future__ import annotations

import json
import math
import os
from uuid import uuid4

from ..state import GameSnapshot


class SessionRcon:
    """Keep FLE's executable Lua state out of Factorio's saved storage."""

    def __init__(self, client):
        self.client = client

    def __getattr__(self, name):
        return getattr(self.client, name)

    @staticmethod
    def scoped(command):
        for prefix in ("/sc ", "/c ", "/silent-command ", "/command "):
            if command.startswith(prefix):
                return prefix + "local storage = jev_fle_runtime; " + command[len(prefix):]
        return command

    def send_command(self, command):
        return self.client.send_command(self.scoped(command))

    def send_commands(self, commands):
        return self.client.send_commands({
            key: self.scoped(command) for key, command in commands.items()
        })


class FleBackend:
    def __init__(self) -> None:
        self._instance = None
        self._resources = {}
        self._drill = None
        self._error = ""

    def start(self, resume: bool = False) -> None:
        from factorio_rcon import RCONClient
        from fle.env import FactorioInstance

        password = os.environ.get("FACTORIO_RCON_PASSWORD")
        if not password:
            raise ValueError("Set FACTORIO_RCON_PASSWORD in .env")

        class DedicatedInstance(FactorioInstance):
            @staticmethod
            def connect_to_server(address, tcp_port):
                client = RCONClient(address, tcp_port, password, timeout=120)
                marker = (client.send_command(
                    "/sc rcon.print(storage.jev_factorio_session == true)"
                ) or "").strip()
                if marker != "true":
                    client.close()
                    raise RuntimeError(
                        "Refusing to initialize an unmarked world. Use a dedicated "
                        "agent save and set storage.jev_factorio_session = true via RCON."
                    )
                if resume:
                    ready = client.send_command(
                        "/sc rcon.print(jev_fle_runtime ~= nil and "
                        "jev_fle_runtime.agent_characters ~= nil and "
                        "jev_fle_runtime.agent_characters[1] ~= nil and "
                        "jev_fle_runtime.agent_characters[1].valid)"
                    )
                    if (ready or "").strip() != "true":
                        client.close()
                        raise RuntimeError("No live agent session to resume; refusing to reset.")
                else:
                    client.send_command("/sc jev_fle_runtime = {jev_session_id="
                                        + json.dumps(uuid4().hex) + "}")
                return SessionRcon(client), address

            def initialise(self, *args, **kwargs):
                if not resume:
                    return super().initialise(*args, **kwargs)

            def _generate_chunks(self, center_x=0, center_y=0, chunk_radius=25):
                """Bound initial terrain generation for the bootstrap scenario."""
                return super()._generate_chunks(center_x, center_y, min(chunk_radius, 8))

        self._instance = DedicatedInstance(
            address=os.environ.get("FACTORIO_RCON_HOST", "127.0.0.1"),
            tcp_port=int(os.environ.get("FACTORIO_RCON_PORT", "27018")),
            fast=True,
            inventory={"burner-mining-drill": 1, "wooden-chest": 1},
            all_technologies_researched=False,
            clear_entities=True,
            peaceful=True,
            reset_speed=1,
        )

    @property
    def _tools(self):
        if self._instance is None:
            raise RuntimeError("Start the FLE backend before using it")
        return self._instance.namespace

    def observe(self) -> GameSnapshot:
        from fle.env import Prototype, Resource

        tools = self._tools
        raw = self._instance.rcon_client.send_command(
            "/sc local agent = storage.agent_characters[1]; "
            "rcon.print(helpers.table_to_json({tick=game.tick,"
            "session_id=storage.jev_session_id,"
            "position={agent.position.x,agent.position.y}}))"
        )
        live = json.loads(raw)
        position = tuple(live["position"])
        inventory = dict(tools.inspect_inventory().items())
        nearby = {}
        alerts = [self._error] if self._error else []
        self._resources = {}
        for name, resource in (("coal", Resource.Coal), ("iron-ore", Resource.IronOre)):
            try:
                target = tools.nearest(resource)
                self._resources[name] = target
                nearby[name] = math.hypot(target.x - position[0], target.y - position[1])
            except Exception as error:
                alerts.append(f"{name}: {error}")
        entities = tools.get_entities({Prototype.BurnerMiningDrill, Prototype.WoodenChest})
        drills = [entity for entity in entities if entity.name == "burner-mining-drill"]
        self._drill = drills[0] if drills else None
        output_chests = [
            entity for entity in entities
            if self._drill is not None and entity.name == "wooden-chest"
            and math.hypot(entity.position.x - self._drill.drop_position.x,
                           entity.position.y - self._drill.drop_position.y) <= 0.1
        ]
        collected = sum(tools.inspect_inventory(entity).get("iron-ore", 0)
                        for entity in output_chests)
        return GameSnapshot(
            tick=live["tick"],
            session_id=live.get("session_id", ""),
            world_kind="fle",
            player_position=position,
            inventory=inventory,
            nearby_resources=nearby,
            placed_entities=[entity.name for entity in entities],
            alerts=alerts,
            drill_status=self._drill.status.value if self._drill else "",
            drill_fuel=self._drill.fuel.get("coal", 0) if self._drill else 0,
            drill_output_connected=bool(output_chests),
            iron_ore_collected=collected,
        )

    def act(self, action: str) -> str:
        from fle.env import Direction, Prototype

        tools = self._tools
        self._error = ""
        try:
            if action == "idle":
                return "Waiting for production"
            if action in ("walk_to_coal", "walk_to_iron"):
                resource = "coal" if action == "walk_to_coal" else "iron-ore"
                position = tools.move_to(self._resources[resource])
                return f"Moved to {resource} at ({position.x}, {position.y})"
            if action in ("mine_coal", "mine_iron"):
                resource = "coal" if action == "mine_coal" else "iron-ore"
                amount = tools.harvest_resource(self._resources[resource], quantity=5)
                return f"Harvested {amount} {resource}"
            if action == "place_burner_drill":
                self._drill = tools.place_entity(
                    Prototype.BurnerMiningDrill,
                    direction=Direction.UP,
                    position=self._resources["iron-ore"],
                    exact=False,
                )
                tools.place_entity(Prototype.WoodenChest, position=self._drill.drop_position)
                return "Placed burner drill on iron with an output chest"
            if action == "fuel_drill":
                if self._drill is None:
                    raise ValueError("No burner drill exists")
                amount = min(5, tools.inspect_inventory().get("coal", 0))
                if amount == 0:
                    raise ValueError("No coal in inventory")
                tools.insert_item(Prototype.Coal, self._drill, quantity=amount)
                return f"Fueled burner drill with {amount} coal"
            if action == "craft_stone_furnace":
                tools.craft_item(Prototype.StoneFurnace, quantity=1)
                return "Crafted a stone furnace"
            raise ValueError(f"Unsupported action: {action}")
        except Exception as error:
            self._error = f"{action} failed: {error}"
            return self._error
