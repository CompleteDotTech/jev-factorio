"""Synthetic captured evidence; no native-game or provider validation claims."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import subprocess
import sys

import pytest

from jev_factorio.replay import EVENT_SCHEMA, ReplayInputError, cli, replay_log


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False).encode("utf-8")


def digest(value):
    return "sha256:" + hashlib.sha256(canonical(value)).hexdigest()


class Capture:
    def __init__(self):
        self.manifest = {"schema_version": 1, "run_id": "synthetic-run", "world_kind": "mock",
                         "git": {"commit": "synthetic-fixture-not-a-native-run"}}
        self.events = []
        self.segment = "segment-1"
        self.add("run_started", {"manifest_sha256": digest(self.manifest)})

    def add(self, kind, payload, decision=None, **correlation):
        event = {
            "schema": EVENT_SCHEMA, "run_id": "synthetic-run", "segment_id": self.segment,
            "sequence": len(self.events) + 1, "event_type": kind,
            "time": {"utc": "2026-09-21T00:00:00Z", "monotonic_ns": len(self.events),
                     "factorio_tick": 10 * len(self.events)},
            "correlation": {**({"decision_id": decision} if decision else {}), **correlation},
            "payload": payload, "prev_hash": None,
        }
        self.events.append(event)
        return event

    def decision(self, decision="d1", *, model=True, error=False, plan="gather", action="mine_coal"):
        before, after, candidates = decision + ":before", decision + ":after", decision + ":candidates"
        call, attempt = decision + ":call", decision + ":attempt"
        plan_data = {"id": plan, "steps": [{"action": action, "parameters": {"count": 5}}]}
        self.add("observation", {"observation_id": before, "state": {"inventory": {"coal": 0}}}, decision)
        self.add("candidate_set_created", {"candidate_set_id": candidates, "observation_id": before,
                                            "candidates": [deepcopy(plan_data)]}, decision)
        if model:
            self.add("model_request", {"observation_id": before, "candidate_set_id": candidates,
                                        "state": {"facts": {"coal": 0}},
                                        "questions": {"candidate": {"type": "choice", "criteria": {plan: "gather"}}}},
                     decision, model_call_id=call)
            if error:
                self.add("provider_error", {"error_type": "timeout"}, decision, model_call_id=call)
            else:
                self.add("model_response", {"answers": {"candidate": {"choice": plan}},
                                             "resolved_model": "synthetic", "usage": None}, decision, model_call_id=call)
        self.add("decision", {"observation_id": before, "candidate_set_id": candidates,
                               "model_called": model, "model_call_id": call if model else None,
                               "plan_id": None if error else plan,
                               "source": "observe" if error else "jev" if model else "deterministic"}, decision)
        if error:
            return
        self.add("plan_committed", {"plan": deepcopy(plan_data)}, decision, plan_id=plan)
        self.add("action_prepared", {"observation_id": before, "action": action, "parameters": {"count": 5},
                                      "plan_id": plan, "step_index": 0}, decision, action_id=attempt)
        self.add("action_dispatched", {}, decision, action_id=attempt)
        self.add("action_returned", {"outcome": "acknowledged, not a verification"}, decision, action_id=attempt)
        self.add("observation", {"observation_id": after, "state": {"inventory": {"coal": 5}}}, decision)
        self.add("verification", {"observation_id": after, "verified": True,
                                   "evidence": {"predicate": "inventory.coal >= 5", "observed": 5}}, decision, action_id=attempt)

    def finish(self):
        self.add("run_finished", {"reason": "synthetic test complete"})

    def write(self, root):
        root.mkdir(exist_ok=True, parents=True)
        (root / "manifest.json").write_text(json.dumps(self.manifest), encoding="utf-8")
        previous = None
        for i, event in enumerate(self.events, 1):
            event["sequence"] = i
            event["prev_hash"] = previous
            event.pop("event_hash", None)
            event["event_hash"] = digest(event)
            previous = event["event_hash"]
        (root / "events.jsonl").write_bytes(b"".join(canonical(event) + b"\n" for event in self.events))
        return root

    def of_type(self, name):
        return next(event for event in self.events if event["event_type"] == name)


@pytest.fixture
def captured():
    result = Capture()
    result.decision()
    result.finish()
    return result


def codes(report):
    return {finding.code for finding in report.findings}


def test_complete_capture_is_deterministic_and_keeps_all_evidence(tmp_path, captured):
    path = captured.write(tmp_path / "run")
    first = replay_log(path)
    assert first.status == "complete"
    assert first.to_dict() == replay_log(path).to_dict()
    assert first.to_dict()["decision_count"] == 1
    assert len(first.events) == len(captured.events)
    decision = first.decisions[0]
    assert decision["selection"]["payload"]["plan_id"] == "gather"
    assert decision["model_calls"][0]["request"]["payload"]["questions"] == captured.of_type("model_request")["payload"]["questions"]
    assert decision["model_calls"][0]["responses"][0]["payload"] == captured.of_type("model_response")["payload"]
    assert decision["actions"][0]["acknowledgment"] == "returned"
    assert decision["actions"][0]["verified"] is True
    assert first.to_dict()["replay_authorized"] is False
    assert first.to_dict()["behavior_reexecuted"] is False
    assert first.to_dict()["authenticity_established"] is False


def test_external_head_and_bound_manifest(tmp_path, captured):
    path = captured.write(tmp_path / "run")
    head = captured.events[-1]["event_hash"]
    assert replay_log(path, expected_head=head).integrity["anchored"] is True
    assert "head_mismatch" in codes(replay_log(path, expected_head="sha256:" + "f" * 64))
    manifest = path / "manifest.json"
    manifest.write_text(json.dumps({**captured.manifest, "condition": "changed"}))
    assert "manifest_hash_mismatch" in codes(replay_log(path))


@pytest.mark.parametrize("mutate,expected", [
    (lambda e: e.update(sequence=999), "sequence_mismatch"),
    (lambda e: e.update(event_hash="sha256:" + "0" * 64), "hash_mismatch"),
    (lambda e: e["payload"].update(extra="tampered"), "hash_mismatch"),
    (lambda e: e.update(prev_hash=None), "hash_mismatch"),
    (lambda e: e.update(run_id="other-run"), "mixed_run"),
    (lambda e: e.update(schema="future.event.v99"), "unsupported_envelope"),
    (lambda e: e.update(line=99999), "unsupported_envelope"),
    (lambda e: e.update(correlation=[]), "unsupported_envelope"),
    (lambda e: e.update(sequence=True), "unsupported_envelope"),
    (lambda e: e["time"].update(monotonic_ns=True), "unsupported_envelope"),
    (lambda e: e["time"].update(utc="2026-09-21T00:00:00"), "unsupported_envelope"),
])
def test_integrity_rejects_changed_records_and_retains_verified_prefix(tmp_path, captured, mutate, expected):
    path = captured.write(tmp_path / "run")
    mutate(captured.events[3])
    (path / "events.jsonl").write_bytes(b"".join(canonical(event) + b"\n" for event in captured.events))
    report = replay_log(path)
    assert expected in codes(report)
    assert report.status == "invalid"
    assert report.integrity["verified_events"] == 3


@pytest.mark.parametrize("tail", [b'{"broken":', b'{"x":1,"x":2}\n', b'{"x":NaN}\n',
                                  b'{"x":Infinity}\n', b'{"x":1e9999}\n', b'[]\n', b'\xff\n', b'\n'])
def test_bad_tail_is_not_silently_ignored_or_anchored(tmp_path, captured, tail):
    path = captured.write(tmp_path / "run")
    with (path / "events.jsonl").open("ab") as stream:
        stream.write(tail)
    report = replay_log(path, expected_head=captured.events[-1]["event_hash"])
    assert report.status == "invalid"
    assert report.integrity["anchored"] is False
    assert report.integrity["digest_scope"] == "consumed_prefix"
    assert len(report.decisions) == 1


def test_whole_event_suffix_loss_is_incomplete_without_anchor(tmp_path, captured):
    path = captured.write(tmp_path / "run")
    head = captured.events[-1]["event_hash"]
    captured.events.pop()
    captured.write(path)
    assert replay_log(path).status == "incomplete"
    assert "missing_run_finish" in codes(replay_log(path))
    assert "head_mismatch" in codes(replay_log(path, expected_head=head))


def test_missing_final_newline_is_reported(tmp_path, captured):
    path = captured.write(tmp_path / "run")
    source = path / "events.jsonl"
    source.write_bytes(source.read_bytes().rstrip(b"\n"))
    assert "unterminated_record" in codes(replay_log(path))


@pytest.mark.parametrize("kind,patch,code", [
    ("decision", {"plan_id": "not-offered"}, "selection_not_offered"),
    ("decision", {"model_called": False}, "model_attribution_conflict"),
    ("decision", {"observation_id": "absent"}, "dangling_reference"),
    ("decision", {"observation_id": "d1:after"}, "noncausal_reference"),
    ("action_prepared", {"parameters": {"count": 99}}, "plan_step_mismatch"),
    ("action_prepared", {"step_index": 99}, "invalid_plan_step"),
    ("action_prepared", {"step_index": True}, "unknown_plan_step"),
    ("action_prepared", {"plan_id": "other"}, "dispatch_plan_mismatch"),
    ("verification", {"observation_id": "d1:before"}, "stale_verification"),
    ("verification", {"verified": "yes"}, "invalid_verification"),
    ("candidate_set_created", {"candidates": [{"id": "gather"}, {"id": "gather"}]}, "duplicate_candidate"),
    ("candidate_set_created", {"candidates": [{"id": []}]}, "invalid_candidates"),
])
def test_rehashed_semantic_mismatches_are_detected(tmp_path, captured, kind, patch, code):
    captured.of_type(kind)["payload"].update(patch)
    report = replay_log(captured.write(tmp_path / "run"))
    assert code in codes(report)


def test_pending_and_lost_acknowledgment_never_authorize_retry(tmp_path, captured):
    captured.events = [event for event in captured.events if event["event_type"] not in {"action_returned", "verification"}]
    report = replay_log(captured.write(tmp_path / "run"))
    assert report.status == "incomplete"
    assert {"unknown_acknowledgment", "unverified_action"} <= codes(report)
    action = report.decisions[0]["actions"][0]
    assert action["acknowledgment"] == "unknown" and action["verified"] is None
    assert report.to_dict()["replay_authorized"] is False


def test_delayed_verification_preserves_lost_ack(tmp_path, captured):
    captured.events.remove(captured.of_type("action_returned"))
    report = replay_log(captured.write(tmp_path / "run"))
    action = report.decisions[0]["actions"][0]
    assert action["verified"] is True and action["acknowledgment"] == "unknown"
    assert report.status == "incomplete"


@pytest.mark.parametrize("kind,code", [("model_response", "duplicate_model_result"),
                                       ("action_returned", "duplicate_action_result"),
                                       ("action_dispatched", "duplicate_dispatch"),
                                       ("decision", "duplicate_decision")])
def test_duplicate_terminal_results_are_not_deduplicated_away(tmp_path, captured, kind, code):
    event = captured.of_type(kind)
    captured.events.insert(captured.events.index(event) + 1, deepcopy(event))
    report = replay_log(captured.write(tmp_path / "run"))
    assert code in codes(report) and report.status == "invalid"


def test_conflicting_verification_is_not_hidden(tmp_path, captured):
    verification = deepcopy(captured.of_type("verification"))
    verification["payload"]["verified"] = False
    captured.events.insert(-1, verification)
    assert "conflicting_verification" in codes(replay_log(captured.write(tmp_path / "run")))


def test_multiple_decisions_reuse_domain_plan_names_not_attempt_ids(tmp_path):
    captured = Capture()
    captured.decision("first", model=False)
    captured.decision("second", model=True)
    captured.finish()
    report = replay_log(captured.write(tmp_path / "run"))
    assert report.status == "complete", report.to_dict()["findings"]
    assert len(report.decisions) == 2
    assert report.decisions[0]["model_calls"] == []
    assert report.decisions[0]["actions"][0]["action_id"] != report.decisions[1]["actions"][0]["action_id"]


def test_provider_failure_abstention_is_a_reconstructable_decision(tmp_path):
    captured = Capture()
    captured.decision(error=True)
    captured.finish()
    report = replay_log(captured.write(tmp_path / "run"))
    assert report.status == "complete"
    assert report.decisions[0]["actions"] == []
    assert report.decisions[0]["model_calls"][0]["errors"]


def test_model_responses_remain_raw_even_when_policy_rejects_them(tmp_path, captured):
    captured.of_type("model_response")["payload"]["answers"] = ["invalid provider shape"]
    report = replay_log(captured.write(tmp_path / "run"))
    assert report.decisions[0]["model_calls"][0]["responses"][0]["payload"]["answers"] == ["invalid provider shape"]
    assert report.to_dict()["behavior_reexecuted"] is False


def test_unknown_events_are_preserved_but_not_certified(tmp_path, captured):
    captured.events.insert(-1, deepcopy(captured.of_type("observation")))
    captured.events[-2]["event_type"] = "future_event"
    report = replay_log(captured.write(tmp_path / "run"))
    assert "unsupported_event" in codes(report)
    assert any(event["event_type"] == "future_event" for event in report.events)
    assert report.status == "incomplete"


def test_orphans_are_preserved_not_attached_by_similar_ticks(tmp_path, captured):
    captured.of_type("verification")["correlation"]["decision_id"] = "other-decision"
    report = replay_log(captured.write(tmp_path / "run"))
    assert "decision_mismatch" in codes(report)
    assert len(report.decisions) == 2
    assert report.decisions[0]["actions"][0]["verified"] is None
    assert report.decisions[1]["actions"] == []


def test_shared_observation_is_resolved_by_explicit_reference(tmp_path, captured):
    captured.of_type("observation")["correlation"] = {}
    report = replay_log(captured.write(tmp_path / "run"))
    assert report.status == "complete"
    assert report.decisions[0]["observations"][0]["payload"]["observation_id"] == "d1:before"


def test_code_revision_segments_remain_separate(tmp_path):
    captured = Capture()
    captured.decision("d1")
    captured.segment = "segment-2"
    captured.add("code_revision_changed", {"previous_segment_id": "segment-1", "from_revision": "A", "to_revision": "B"})
    captured.decision("d1")  # Explicit new segment; reused local IDs are not joined.
    captured.finish()
    report = replay_log(captured.write(tmp_path / "run"))
    assert report.status == "complete"
    assert report.to_dict()["mixed_segments"] is True
    assert len(report.decisions) == 2
    assert report.decisions[0]["segment_id"] != report.decisions[1]["segment_id"]


@pytest.mark.parametrize("event_type", ["operational_repair", "manual_intervention", "incident_started", "checkpoint_written"])
def test_supervisor_evidence_is_retained_without_new_decisions(tmp_path, captured, event_type):
    captured.events.insert(-1, deepcopy(captured.of_type("run_started")))
    captured.events[-2].update(event_type=event_type, payload={"incident": "synthetic"})
    report = replay_log(captured.write(tmp_path / "run"))
    assert report.status == "complete"
    assert len(report.decisions) == 1
    assert report.events[-2]["event_type"] == event_type


def test_unannounced_revision_and_after_finish_are_invalid(tmp_path, captured):
    captured.add("code_revision_changed", {"from_revision": "A", "to_revision": "B"})
    report = replay_log(captured.write(tmp_path / "run"))
    assert {"revision_without_segment", "after_run_finished"} <= codes(report)


def legacy_record(hierarchical=False):
    row = {"tick": 1, "action": "mine_coal", "state": {"inventory": {"coal": 0}},
           "after_state": {"inventory": {"coal": 5}}, "outcome": "captured acknowledgment"}
    if hierarchical:
        row.update(schema_version=1, controller="hierarchical", session_id="mock-session", model_call=True,
                   verified=True, decision={"plan_id": "gather", "source": "mock", "model_called": True,
                   "state": {"candidate_plans": {"gather": {"id": "gather", "steps": []}}},
                   "questions": {"candidate": {}}, "answers": {"candidate": {"choice": "gather"}}})
    else:
        row.update(source="jev", questions={"next_action": {"criteria": {"mine_coal": "gather", "idle": "wait"}}},
                   answers={"next_action": {"choice": "mine_coal"}}, confidence=0.9)
    return row


@pytest.mark.parametrize("hierarchical", [False, True])
def test_legacy_logs_never_fabricate_missing_evidence_or_unique_counts(tmp_path, hierarchical):
    path = tmp_path / "legacy.jsonl"
    record = legacy_record(hierarchical)
    path.write_text(json.dumps(record) + "\n")
    report = replay_log(path)
    assert report.status == "incomplete" and report.format == "legacy"
    assert report.to_dict()["decision_count"] is None
    assert report.to_dict()["legacy_record_count"] == 1
    assert report.decisions[0]["record"] == record
    assert report.decisions[0]["dispatch"]["prepared_event"] is None
    assert report.decisions[0]["verification"]["recorded_verdict"] is (True if hierarchical else None)


def test_legacy_polling_and_repeated_history_are_not_inferred_actions(tmp_path):
    record = legacy_record(True)
    record.update(decision=None, action="observe", verified=False, model_call=False,
                  history=[{"event": "step_verified", "plan": "reused"}])
    path = tmp_path / "polling.jsonl"
    path.write_text((json.dumps(record) + "\n") * 2)
    report = replay_log(path)
    assert report.to_dict()["decision_count"] is None
    assert all(not frame["selection_recorded"] for frame in report.decisions)
    assert all(frame["decision_id"] is None for frame in report.decisions)
    assert report.to_dict()["legacy_record_count"] == 2


@pytest.mark.parametrize("patch", [{"decision": []}, {"decision": {"plan_id": []}},
                                    {"schema_version": True}, {"schema_version": 500}])
def test_malformed_legacy_fields_do_not_crash(tmp_path, patch):
    path = tmp_path / "legacy.jsonl"
    path.write_text(json.dumps({**legacy_record(True), **patch}) + "\n")
    assert replay_log(path).status == "invalid"


@pytest.mark.parametrize("options", [{"max_events": 2}, {"max_line_bytes": 16}, {"max_input_bytes": 32}])
def test_bounded_input(tmp_path, captured, options):
    path = captured.write(tmp_path / "run") / "events.jsonl"
    assert "input_limit" in codes(replay_log(path, **options))


@pytest.mark.parametrize("options", [{"max_events": 0}, {"max_line_bytes": True},
                                      {"expected_head": "not-a-hash"}, {"expected_head": False}])
def test_bad_options_raise_public_input_error(tmp_path, options):
    with pytest.raises(ReplayInputError):
        replay_log(tmp_path / "missing", **options)


def test_no_missing_or_empty_log_is_reported_complete(tmp_path):
    with pytest.raises(ReplayInputError):
        replay_log(tmp_path / "missing")
    path = tmp_path / "empty.jsonl"
    path.touch()
    assert replay_log(path).status == "incomplete"


def test_cli_exit_codes_and_read_only_output(tmp_path, captured, capsys):
    path = captured.write(tmp_path / "run")
    original = {file.name: file.read_bytes() for file in path.iterdir()}
    assert cli([str(path)]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "complete"
    assert cli([str(path), "--output", str(path / "events.jsonl")]) == 2
    assert {file.name: file.read_bytes() for file in path.iterdir()} == original
    assert cli([str(path / "events.jsonl")]) == 3  # No adjacent manifest auto-discovery.
    assert cli([str(path / "events.jsonl"), "--allow-incomplete"]) == 0
    output = tmp_path / "report.json"
    assert cli([str(path), "--output", str(output)]) == 0
    assert json.loads(output.read_text())["replay_authorized"] is False
    captured.of_type("decision")["payload"]["plan_id"] = "invalid"
    captured.write(path)
    assert cli([str(path), "--allow-incomplete", "--output", str(tmp_path / "bad.json")]) == 1


def test_replay_import_and_execution_are_offline_in_a_fresh_process(tmp_path, captured):
    path = captured.write(tmp_path / "run")
    source = Path(__file__).resolve().parents[1] / "src"
    script = r'''
import importlib.abc, json, socket, subprocess, sys, os
sys.path.insert(0, sys.argv[1])
class BlockLiveImports(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.startswith(("requests", "dotenv", "typesafe", "fle", "jev_factorio.backends",
                                "jev_factorio.jev_client", "jev_factorio.main", "jev_factorio.controller")):
            raise AssertionError("live client import")
sys.meta_path.insert(0, BlockLiveImports())
def forbidden(*args, **kwargs):
    raise AssertionError("side effect")
socket.socket = forbidden
socket.create_connection = forbidden
subprocess.Popen = forbidden
os.system = forbidden
from jev_factorio.replay import replay_log
result = replay_log(sys.argv[2]).to_dict()
assert result["status"] == "complete", result["findings"]
assert not result["replay_authorized"]
print(json.dumps({"status": result["status"]}))
'''
    completed = subprocess.run([sys.executable, "-I", "-c", script, str(source), str(path)],
                               text=True, capture_output=True, check=True, timeout=15)
    assert json.loads(completed.stdout)["status"] == "complete"


def test_dotenv_and_artifact_urls_are_never_opened(tmp_path, captured, monkeypatch):
    captured.manifest["artifact"] = "https://invalid.example/never-fetch"
    captured.events[0]["payload"]["manifest_sha256"] = digest(captured.manifest)
    path = captured.write(tmp_path / "run")
    (tmp_path / ".env").write_text("TYPESAFE_API_KEY=do-not-read")
    monkeypatch.chdir(tmp_path)
    original_open = Path.open
    allowed = {path / "manifest.json", path / "events.jsonl"}
    def checked_open(self, *args, **kwargs):
        assert self in allowed
        assert not args or args[0] == "rb"
        return original_open(self, *args, **kwargs)
    monkeypatch.setattr(Path, "open", checked_open)
    assert replay_log(path).status == "complete"


def test_selected_work_without_dispatch_or_explanation_is_incomplete(tmp_path, captured):
    stop = captured.events.index(captured.of_type("action_prepared"))
    captured.events = captured.events[:stop]
    captured.finish()
    report = replay_log(captured.write(tmp_path / "run"))
    assert "missing_execution_evidence" in codes(report)
    assert report.status == "incomplete"


def test_no_dispatch_plan_verification_reconstructs_without_an_action(tmp_path, captured):
    captured.events = [event for event in captured.events if event["event_type"] not in {
        "action_prepared", "action_dispatched", "action_returned"
    }]
    verification = captured.of_type("verification")
    verification["correlation"].pop("action_id")
    verification["payload"].update(scope="plan", plan_id="gather")
    report = replay_log(captured.write(tmp_path / "run"))
    assert report.status == "complete", report.to_dict()["findings"]
    assert report.decisions[0]["actions"] == []
    assert report.decisions[0]["verifications"][0]["payload"]["verified"] is True


def test_aborted_selected_work_has_explicit_termination(tmp_path, captured):
    stop = captured.events.index(captured.of_type("action_prepared"))
    captured.events = captured.events[:stop]
    captured.add("decision_finished", {"status": "aborted", "reason": "Captured precondition changed"}, "d1")
    captured.finish()
    report = replay_log(captured.write(tmp_path / "run"))
    assert report.status == "complete"
    assert report.decisions[0]["actions"] == []
    assert report.decisions[0]["termination"]["payload"]["status"] == "aborted"


def test_parameter_shape_mutations_return_findings_not_exceptions(tmp_path, captured):
    # Exercise untrusted types, not just happy-path fixture fields.
    for index, event in enumerate(captured.events):
        for key in event["payload"]:
            for value in (None, [], {}, True, 0, "unexpected-shape"):
                altered = deepcopy(captured)
                altered.events[index]["payload"][key] = value
                report = replay_log(altered.write(tmp_path / "run"))
                assert report.status in {"complete", "incomplete", "invalid"}


def test_mixed_input_formats_are_rejected(tmp_path, captured):
    path = captured.write(tmp_path / "run")
    with (path / "events.jsonl").open("ab") as stream:
        stream.write(canonical(legacy_record()) + b"\n")
    assert "unsupported_envelope" in codes(replay_log(path))
    (path / "legacy.jsonl").write_bytes(canonical(legacy_record()) + b"\n" + canonical(captured.events[0]) + b"\n")
    assert "mixed_format" in codes(replay_log(path / "legacy.jsonl"))


def test_excessive_integer_precision_is_rejected(tmp_path):
    path = tmp_path / "bad.jsonl"
    path.write_bytes(b'{"number":' + b'1' * 129 + b'}\n')
    assert "invalid_json" in codes(replay_log(path))


def test_cli_bounds_are_configurable_and_do_not_mask_failures(tmp_path, captured, capsys):
    path = captured.write(tmp_path / "run")
    assert cli([str(path), "--max-events", "1", "--allow-incomplete"]) == 1
    assert json.loads(capsys.readouterr().out)["status"] == "invalid"
    assert cli([str(path), "--max-events", "0"]) == 2


def test_source_line_cannot_be_spoofed_in_input(tmp_path, captured):
    captured.events[0]["line"] = 1000000
    report = replay_log(captured.write(tmp_path / "run"))
    assert "unsupported_envelope" in codes(report)
    assert report.findings[0].line == 1


def test_legacy_chain_head_cannot_be_asserted(tmp_path):
    path = tmp_path / "legacy.jsonl"
    path.write_bytes(canonical(legacy_record()) + b"\n")
    report = replay_log(path, expected_head="sha256:" + "a" * 64)
    assert "unverifiable_head" in codes(report)
    assert report.status == "invalid"


def test_missing_payloads_are_gaps_not_inferred_values(tmp_path, captured):
    captured.of_type("model_response")["payload"].pop("answers")
    captured.of_type("model_request")["payload"].pop("questions")
    captured.of_type("observation")["payload"].pop("state")
    report = replay_log(captured.write(tmp_path / "run"))
    assert {"missing_answers", "missing_request", "missing_state"} <= codes(report)
    assert report.status == "incomplete"


def test_committed_plan_cannot_change_the_offered_definition(tmp_path, captured):
    captured.of_type("plan_committed")["payload"]["plan"]["steps"][0]["parameters"] = {"count": 99}
    captured.of_type("action_prepared")["payload"]["parameters"] = {"count": 99}
    assert "committed_plan_changed" in codes(replay_log(captured.write(tmp_path / "run")))


def test_late_model_result_cannot_be_the_cause_of_an_earlier_selection(tmp_path, captured):
    response = captured.of_type("model_response")
    captured.events.remove(response)
    captured.events.insert(-1, response)
    report = replay_log(captured.write(tmp_path / "run"))
    assert "noncausal_model_result" in codes(report)
    assert report.status == "invalid"


def test_bundled_fixture_has_a_fixed_independent_head():
    path = Path(__file__).parent / "fixtures" / "replay" / "event-v1"
    expected = "sha256:17a897177b90abf490de9db9a55eee6c22f23d2dc9c0309a5c7d8f9fae3601c8"
    report = replay_log(path, expected_head=expected)
    assert report.status == "complete"
    assert report.integrity["anchored"] is True
    assert len(report.decisions) == 2
