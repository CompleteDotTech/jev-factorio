"""Receipt-focused test doubles; no game, model service, or native initialization."""
from copy import deepcopy

from jev_factorio.controller import HierarchicalLoop
from jev_factorio.skills import Plan, Step
from jev_factorio.state import GameSnapshot


class ReceiptBackend:
    def __init__(self, mode="immediate"):
        self.mode = mode
        self.calls = []
        self.queued = None
        self.fail_observation = False
        self.state = GameSnapshot(
            session_id="receipt-session", world_kind="mock", tick=10,
            inventory={"coal": 5, "automation-science-pack": 20},
            factory={"entities": {"utility:lab": {"unit_number": 7, "input": {}}},
                     "receipts": {}, "produced": {"iron-plate": 100}},
        )

    def observe(self):
        if self.fail_observation:
            self.fail_observation = False
            raise TimeoutError("secret observation response must not be logged")
        return deepcopy(self.state)

    def act(self, action):
        assert action == "idle"
        self.state.tick += 1
        return "synthetic wait"

    def execute(self, action, parameters):
        self.calls.append((action, deepcopy(parameters)))
        self.state.tick += 1
        quantity = 10 if self.mode == "partial" else parameters["quantity"]
        self.queued = {**parameters, "unit_number": 7, "extracting": False, "quantity": quantity}
        if self.mode not in {"delayed", "no_effect"}:
            self.publish()
        if self.mode == "lost_ack":
            raise TimeoutError("Bearer secret-rcon-password")
        if self.mode == "lost_observation":
            self.fail_observation = True
        if self.mode == "interrupt":
            raise KeyboardInterrupt("secret interrupt payload")
        return "synthetic transfer"

    def publish(self):
        receipt = self.queued
        self.state.inventory["automation-science-pack"] -= receipt["quantity"]
        self.state.factory["entities"]["utility:lab"]["input"]["automation-science-pack"] = receipt["quantity"]
        self.state.factory["receipts"][receipt["receipt"]] = deepcopy(receipt)
        self.queued = None


def install_plan(monkeypatch, backend, *, action="factory_insert"):
    def compiler(goal, snapshot):
        if action == "factory_wait":
            step = Step(action, "machine_output", "automation-science-pack", 20,
                        verification={"role": "utility:lab"})
        else:
            parameters = {"role": "utility:lab", "item": "automation-science-pack", "quantity": 20,
                          "receipt": f"transfer:{len(backend.calls)}"}
            step = Step(action, "transfer", parameters=parameters)
        return [Plan("same-plan-id", goal, "Synthetic receipt exercise", (step,))], ""

    monkeypatch.setattr("jev_factorio.controller.compile_plans", compiler)


def controller(tmp_path, backend, *, resume=False, **options):
    return HierarchicalLoop(
        backend, policy="deterministic", target="bootstrap_mining",
        checkpoint=str(tmp_path / "checkpoint.json"), log_file=str(tmp_path / "run.jsonl"),
        resume_controller=resume, tick_seconds=0, **options,
    )
