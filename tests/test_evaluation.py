import json
from copy import deepcopy

import pytest

from jev_factorio.evaluation import summarize
from attempt_helpers import ReceiptBackend, controller, install_plan


def write(path, records):
    path.write_text("".join(json.dumps(record) + "\n" for record in records))


@pytest.mark.parametrize("mode", ["immediate", "delayed", "lost_ack", "lost_observation"])
def test_verified_action_count_is_independent_of_acknowledgment(monkeypatch, tmp_path, mode):
    backend = ReceiptBackend(mode)
    install_plan(monkeypatch, backend)
    loop = controller(tmp_path, backend)
    if mode == "lost_observation":
        with pytest.raises(TimeoutError):
            loop.step()
    else:
        loop.step()
    if mode == "delayed":
        backend.publish()
    if mode != "immediate":
        controller(tmp_path, backend, resume=True).step()
    summary = summarize(tmp_path / "run.jsonl")
    assert summary["verified_actions"] == 1 and summary["verified_waits"] == 0
    assert summary["native_victory_event_observed"] is False
    assert summary["evidence_class"] == "synthetic"
    assert summary["unknown_verification_latencies"] == (mode != "immediate")
    assert len(backend.calls) == 1


def test_duplicate_completion_counted_once_and_conflict_rejected(monkeypatch, tmp_path):
    backend = ReceiptBackend()
    install_plan(monkeypatch, backend)
    record = controller(tmp_path, backend).step()
    path = tmp_path / "duplicate.jsonl"
    write(path, [record, record])
    assert summarize(path)["verified_actions"] == 1
    conflicting = deepcopy(record)
    conflicting["attempt_outcomes"][0]["finished_tick"] += 1
    write(path, [record, conflicting])
    with pytest.raises(ValueError, match="Conflicting"):
        summarize(path)


def test_legacy_logs_do_not_invent_unique_attempts_or_timing(monkeypatch, tmp_path):
    backend = ReceiptBackend()
    install_plan(monkeypatch, backend)
    record = controller(tmp_path, backend).step()
    record["schema_version"] = 1
    for key in ["attempt", "attempt_outcomes", "phases", "recorded_at_utc", "process_id"]:
        del record[key]
    path = tmp_path / "legacy.jsonl"
    write(path, [record, record])
    summary = summarize(path)
    assert summary["verified_actions"] is None
    assert summary["legacy_records_present"]
    assert summary["legacy_verified_action_records"] == 2
    assert summary["verification_latency_seconds"] == []


def test_mixed_versions_preserve_known_counts_and_signal_incompleteness(monkeypatch, tmp_path):
    backend = ReceiptBackend()
    install_plan(monkeypatch, backend)
    current = controller(tmp_path, backend).step()
    old = {**current, "schema_version": 1}
    path = tmp_path / "mixed-versions.jsonl"
    write(path, [old, current])
    result = summarize(path)
    assert result["verified_actions"] is None
    assert result["identified_verified_actions"] == 1


def test_existing_stock_is_not_reported_as_new_production(monkeypatch, tmp_path):
    backend = ReceiptBackend()
    install_plan(monkeypatch, backend)
    record = controller(tmp_path, backend).step()
    record["after_state"]["factory"]["produced"]["iron-plate"] = 110
    path = tmp_path / "production.jsonl"
    write(path, [record])
    assert summarize(path)["observed_production_delta"] == {"iron-plate": 10}
    record["after_state"]["factory"]["produced"]["iron-plate"] = 90
    write(path, [record])
    assert summarize(path)["observed_production_delta"] is None


def test_future_schema_and_missing_v2_fields_fail_closed(monkeypatch, tmp_path):
    backend = ReceiptBackend()
    install_plan(monkeypatch, backend)
    record = controller(tmp_path, backend).step()
    path = tmp_path / "invalid.jsonl"
    for change in ({"schema_version": 3}, {"schema_version": True}, {"attempt_outcomes": None}):
        write(path, [{**record, **change}])
        with pytest.raises(ValueError):
            summarize(path)


def test_sessions_cannot_be_combined(monkeypatch, tmp_path):
    backend = ReceiptBackend()
    install_plan(monkeypatch, backend)
    record = controller(tmp_path, backend).step()
    path = tmp_path / "mixed.jsonl"
    write(path, [record, {**record, "session_id": "another-world"}])
    with pytest.raises(ValueError, match="mix"):
        summarize(path)
