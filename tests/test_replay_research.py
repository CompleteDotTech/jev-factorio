"""Integration with the canonical writer and controller trace, never a live game."""
import json

import pytest

from jev_factorio.replay import replay_log


def capture(tmp_path):
    from jev_factorio.research_log import ResearchLog, RunConfiguration
    from jev_factorio.causal_trace import CausalTrace
    from jev_factorio.backends.mock import MockBackend

    path = tmp_path / "research"
    with ResearchLog(path, RunConfiguration("mock", "flat", "jev"), environ={}) as sink:
        trace = CausalTrace(sink, "flat")
        trace.begin_step()
        trace.observe(MockBackend(), "before_decision")
        trace.emit("candidate_set_created", {"candidates": {"idle": "wait"}, "status": "ok"})
        trace.emit("decision", {"action": "idle", "model_called": False})
        trace.dispatch(lambda: "waited", "idle", role="flat")
        trace.observe(MockBackend(), "after_action")
        trace.emit("verification", {"verified": None, "phase": "flat"})
        trace.emit("step_finished", {"action": "idle"})
    return path


def test_canonical_writer_trace_preserves_source_identity(tmp_path, monkeypatch):
    from jev_factorio.research_log import verify_run
    import subprocess
    import socket

    path = capture(tmp_path)
    expected = verify_run(path)
    before = {entry.name: entry.read_bytes() for entry in path.iterdir()}

    def forbidden(*args, **kwargs):
        raise AssertionError("Replay attempted external execution")

    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(socket, "socket", forbidden)
    report = replay_log(path, format="research-v1", expected_head=expected["final_event_hash"])
    assert report.status == "incomplete", report.to_dict()["findings"]
    assert report.integrity["status"] == "verified_source"
    assert report.integrity["final_event_hash"] == expected["final_event_hash"]
    rows = [json.loads(line) for line in before["events.jsonl"].splitlines()]
    assert [{key: value for key, value in event.items() if key != "line"}
            for event in report.events] == rows
    assert report.decisions[0]["actions"][0]["verified"] is None
    assert before == {entry.name: entry.read_bytes() for entry in path.iterdir()}


@pytest.mark.parametrize("target", ["events.jsonl", "manifest.json", "integrity.json"])
def test_modified_producer_evidence_fails_before_causal_projection(tmp_path, target):
    path = capture(tmp_path)
    file = path / target
    rows = file.read_text().splitlines()
    row = json.loads(rows[0])
    row["run_id"] = "00000000-0000-0000-0000-000000000000"
    rows[0] = json.dumps(row)
    file.write_text("\n".join(rows) + "\n")
    report = replay_log(path, format="research-v1")
    assert report.status == "invalid"
    assert report.decisions == []


def test_resealed_invalid_causal_reference_is_rejected(tmp_path):
    from jev_factorio.research_log import ResearchLog, RunConfiguration
    path = tmp_path / "research"
    with ResearchLog(path, RunConfiguration("mock", "flat", "jev"), environ={}) as sink:
        sink.emit("action_returned", {"trace_id": "trace", "decision_id": "decision:1",
                                     "action_id": "action:missing", "status": "ok"})
    report = replay_log(path, format="research-v1")
    assert report.status == "invalid"
    assert any(finding.code == "invalid_action_reference" for finding in report.findings)
