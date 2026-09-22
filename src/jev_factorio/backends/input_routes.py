"""Native input-route decorator; existing controls own walking and construction."""
from __future__ import annotations

import json
from importlib.resources import files
from types import SimpleNamespace

from ..input_routes import COMMAND, validate
from ..telemetry import Trace, phase


class InputRouteFactory:
    def __init__(self, native) -> None:
        self.native = native
        native.command(files("jev_factorio").joinpath("lua/input_routes.lua").read_text())

    def __getattr__(self, name):
        return getattr(self.native, name)

    def execute(self, action: str, parameters: dict, *, trace: Trace | None = None) -> str:
        if action != COMMAND:
            if trace is None:
                return self.native.execute(action, parameters)
            return self.native.execute(action, parameters, trace=trace)
        validate(parameters)
        with phase("entity_lookup", trace):
            target = json.loads(self.native.call("prepare_input_route", parameters))
        with phase("approach", trace):
            self.native.backend._fair.approach(SimpleNamespace(**target["position"]), target["name"])
        with phase("transfer_rpc", trace):
            self.native.call("build_input_route", parameters)
        return "Native input component returned; paid receipt and end-to-end flow require observation"
