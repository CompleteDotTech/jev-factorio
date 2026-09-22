"""Optional native adapter; ordinary commands retain their original implementation."""
from __future__ import annotations

import json
from importlib.resources import files

from ..output_buffers import COMMAND, PARTS, validate


class OutputBufferFactory:
    def __init__(self, native) -> None:
        self.native = native
        native.command(files("jev_factorio").joinpath("lua/output_buffers.lua").read_text())

    def __getattr__(self, name):
        return getattr(self.native, name)

    def execute(self, action: str, parameters: dict) -> str:
        if action != COMMAND:
            return self.native.execute(action, parameters)
        from fle.env import Position

        validate(parameters)
        # Preparation freezes observed geometry but creates no game entity.
        # The controller has already checkpointed this exact command as pending.
        target = json.loads(self.native.call("prepare_output_buffer", parameters))
        self.native.backend._fair.approach(Position(**target["position"]), PARTS[parameters["part"]])
        # Place + register + paid receipt in one native RPC; never retry here.
        self.native.call("build_output_buffer", parameters)
        return "Paid buffer component returned; placement and flow require observation"
