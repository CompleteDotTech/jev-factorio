"""Full-repository CI checks against records emitted by the actual controllers."""
import hashlib
import json

import pytest

from jev_factorio.replay import replay_log


@pytest.mark.parametrize("controller", ["flat", "hierarchical"])
def test_actual_mock_controller_records_remain_read_only(tmp_path, monkeypatch, controller):
    from jev_factorio.backends.mock import MockBackend
    from jev_factorio.controller import HierarchicalLoop
    from jev_factorio.jev_client import MockJevClient
    from jev_factorio.loop import AgentLoop

    backend, client = MockBackend(), MockJevClient()
    log = tmp_path / "gameplay.jsonl"
    options = {"jev": client, "tick_seconds": 0, "log_file": str(log)}
    loop = (HierarchicalLoop(backend, target="bootstrap_mining", **options)
            if controller == "hierarchical" else AgentLoop(backend, **options))
    loop.run(steps=40 if controller == "hierarchical" else 8)
    before = log.read_bytes()
    records = [json.loads(line) for line in before.splitlines()]
    assert records

    def forbidden(*args, **kwargs):
        raise AssertionError("Replay attempted a provider/backend call")

    monkeypatch.setattr(backend, "observe", forbidden)
    monkeypatch.setattr(backend, "act", forbidden)
    monkeypatch.setattr(client, "evaluate", forbidden)
    report = replay_log(log)
    assert report.status == "incomplete", report.to_dict()["findings"]
    assert report.format == "legacy"
    assert [frame["record"] for frame in report.decisions] == records
    assert report.integrity["input_sha256"] == hashlib.sha256(before).hexdigest()
    assert report.to_dict()["replay_authorized"] is False
    assert report.to_dict()["decision_count"] is None
    assert log.read_bytes() == before
