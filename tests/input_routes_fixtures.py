"""Synthetic evidence shared by contract and actual-controller regression tests."""
from copy import deepcopy
from types import SimpleNamespace

SOURCE = "recipe:iron-plate"
LAYOUT = "input:17:1"


def fixture():
    def machine(name, unit, x, y, **extra):
        return dict(name=name, unit_number=unit, position={"x": x, "y": y}, fuel={"coal": 10},
                    input={}, output={}, products_finished=30, **extra)
    steps = [dict(part="inserter", name="burner-inserter", position={"x": 8.5, "y": 0.5}, direction=4)]
    for n in (3, 2, 1):
        steps.append(dict(part=f"belt:{n}", name="transport-belt", position={"x": n+4.5, "y": 0.5}, direction=4))
    steps.append(dict(part="drill", name="burner-mining-drill", position={"x": 4, "y": 1}, direction=0))
    entities = {SOURCE: machine("stone-furnace", 17, 10, 0),
                "out:chest": machine("wooden-chest", 19, 13.5, 0.5),
                "out:arm": machine("burner-inserter", 18, 12.5, 0.5)}
    output = dict(source=SOURCE, source_unit=17, item="iron-plate", layout="output:17", state="ready",
                  topology=True, held=0, chest_role="out:chest", parts={
                      "chest": dict(role="out:chest", unit_number=19, receipt="out:c", paid=1),
                      "inserter": dict(role="out:arm", unit_number=18, receipt="out:a", paid=1)},
                  flow=dict(layout="output:17", source_unit=17, first_tick=0, last_tick=120,
                            positive_samples=3, received=3, conservation=True))
    row = dict(source=SOURCE, source_unit=17, item="iron-plate", ore="iron-ore", layout=LAYOUT,
               steps=steps, parts={}, state="proposed", topology=False, reserve_belts=0, flow={})
    return SimpleNamespace(session_id="input-test", tick=300,
                           inventory={"transport-belt": 23, "burner-mining-drill": 1, "burner-inserter": 1, "coal": 50},
                           factory=dict(player_connected=True, player_bound=True, crafting_queue=0,
                                        entities=entities, receipts={}, produced={},
                                        output_buffers=dict(protocol=1, session_id="input-test", tick=300, sources={SOURCE: output}),
                                        input_routes=dict(protocol=1, session_id="input-test", tick=300, sources={SOURCE: row})))


def row(snapshot):
    return snapshot.factory["input_routes"]["sources"][SOURCE]


def parameters(snapshot):
    data = row(snapshot)
    spec = next(s for s in data["steps"] if s["part"] not in data["parts"])
    return dict(source=SOURCE, layout=LAYOUT, part=spec["part"], receipt="receipt:"+spec["part"], reserve_belts=20)


def build(snapshot, p=None):
    data = row(snapshot)
    p = p or parameters(snapshot)
    spec = next(s for s in data["steps"] if s["part"] == p["part"])
    unit = 100 + len(data["parts"])
    role = "input:" + p["part"]
    data["parts"][p["part"]] = dict(role=role, unit_number=unit, receipt=p["receipt"], paid=1)
    snapshot.factory["entities"][role] = dict(name=spec["name"], unit_number=unit, position=deepcopy(spec["position"]),
                                             fuel={"coal": 5}, input={}, output={})
    snapshot.inventory[spec["name"]] -= 1
    data["reserve_belts"] = p["reserve_belts"]
    data["state"] = "building"
    if len(data["parts"]) == len(data["steps"]):
        data["state"], data["topology"] = "ready", True


def full(snapshot):
    for _ in row(snapshot)["steps"]:
        build(snapshot)
    return snapshot


def commission(snapshot):
    row(snapshot)["flow"] = dict(layout=LAYOUT, source_unit=17, first_tick=0, last_tick=180,
                                  positive_samples=3, received=3, mined=3, delivered=3, new_plates=3,
                                  conservation=True)
