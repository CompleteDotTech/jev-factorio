"""Paired offline controller regression: metadata must not add gameplay calls."""
from copy import deepcopy
import json

import pytest

from jev_factorio.backends.mock import MockBackend
from jev_factorio.controller import HierarchicalLoop
from jev_factorio.jev_client import MockJevClient
from jev_factorio.loop import AgentLoop
from jev_factorio.provenance import CONTEXT_ENV


class CountingBackend(MockBackend):
    def __init__(self):
        super().__init__()
        self.session_id = "mock:paired-provenance"
        self.calls = []

    def observe(self):
        self.calls.append(("observe",))
        return super().observe()

    def act(self, action):
        self.calls.append(("act", action))
        return super().act(action)


class CountingClient(MockJevClient):
    def __init__(self):
        super().__init__()
        self.calls = []

    def evaluate(self, state, questions):
        self.calls.append(deepcopy((state, questions)))
        return super().evaluate(state, questions)


@pytest.mark.parametrize("hierarchical", [False, True])
def test_provenance_only_adds_context_fields(tmp_path, monkeypatch, hierarchical):
    context = {"run_id": "run-1", "segment_id": "seg-000002", "execution_id": "child-1",
               "code_revision": {"commit": "a" * 40, "source_sha256": "b" * 64}}

    def run(supervised):
        if supervised:
            monkeypatch.setenv(CONTEXT_ENV, json.dumps(context))
        else:
            monkeypatch.delenv(CONTEXT_ENV, raising=False)
        backend, client = CountingBackend(), CountingClient()
        path = tmp_path / ("supervised.jsonl" if supervised else "legacy.jsonl")
        if hierarchical:
            loop = HierarchicalLoop(backend, jev=client, target="bootstrap_mining",
                                    policy="hybrid", log_file=str(path), tick_seconds=0)
        else:
            loop = AgentLoop(backend, jev=client, log_file=str(path), tick_seconds=0)
        # Context is frozen at construction, not reread during each action.
        if supervised:
            monkeypatch.setenv(CONTEXT_ENV, json.dumps({**context, "run_id": "changed-later"}))
        for _ in range(40):
            if getattr(loop, "terminal", False):
                break
            loop.step()
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        return backend.calls, client.calls, rows

    baseline = run(False)
    tagged = run(True)
    assert tagged[:2] == baseline[:2]
    assert tagged[2] and all(all(row[key] == value for key, value in context.items()) for row in tagged[2])
    stripped = [{key: value for key, value in row.items() if key not in context} for row in tagged[2]]
    assert stripped == baseline[2]
    assert all("run_id" not in row for row in baseline[2])
