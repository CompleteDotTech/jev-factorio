"""Summarize evidence logs without relabeling mock runs as live benchmarks."""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

from .telemetry import WAIT_ACTIONS, validate_attempt


def read_records(path: Path) -> list[dict]:
    def invalid_constant(value):
        raise ValueError("Non-finite number in evaluation log")

    with path.open(encoding="utf-8") as stream:
        records = [json.loads(line, parse_constant=invalid_constant) for line in stream if line.strip()]
    if not records:
        raise ValueError("Empty evaluation log")
    if any(not isinstance(r, dict) or r.get("controller") != "hierarchical" for r in records):
        raise ValueError("Do not mix sessions, policies, targets, or log schemas")
    identity = {(r.get("session_id"), r.get("world_kind"), r.get("target"),
                 r.get("policy"), r.get("requested_model")) for r in records}
    if len(identity) != 1:
        raise ValueError("Do not mix sessions, policies, targets, or log schemas")
    return records


def _attempt_counts(records: list[dict]) -> dict:
    identities, finished = {}, {}
    legacy = False
    for record in records:
        version = record.get("schema_version", 1)
        if type(version) is not int or version not in {1, 2}:
            raise ValueError("Unsupported evaluation schema")
        if version == 1:
            legacy = True
            continue
        if "attempt" not in record or not isinstance(record.get("attempt_outcomes"), list):
            raise ValueError("Missing version 2 attempt evidence")
        if len(record["attempt_outcomes"]) > 64:
            raise ValueError("Unbounded attempt outcome history")
        evidence = [(a, True) for a in record["attempt_outcomes"]]
        if record["attempt"] is not None:
            evidence.append((record["attempt"], False))
        for attempt, complete in evidence:
            validate_attempt(attempt, finished=complete)
            key = attempt["id"]
            identity = {k: attempt[k] for k in (
                "origin", "action", "plan_id", "step_index", "step_sha256", "started_tick",
                "started_at_utc", "process_id", "expected_unit_number", "receipt",
            )}
            if key in identities and identities[key] != identity:
                raise ValueError("Conflicting attempt identity")
            identities[key] = identity
            if complete:
                if key in finished and finished[key] != attempt:
                    raise ValueError("Conflicting attempt outcome")
                finished[key] = attempt
    verified = [a for a in finished.values() if a["outcome"] == "verified"]
    actions = sum(a["action"] not in WAIT_ACTIONS for a in verified)
    return {
        "verified_actions": None if legacy else actions,
        "identified_verified_actions": actions,
        "verified_waits": sum(a["action"] in WAIT_ACTIONS for a in verified),
        "identified_attempts": len(identities),
        "legacy_records_present": legacy,
        "attempt_count_scope": "Unique IDs present in log, including carried checkpoint outcomes; not a full-campaign total",
        "legacy_verified_action_records": sum(
            r.get("schema_version", 1) == 1 and r.get("verified") is True
            and r.get("action") not in {"observe", "verify", *WAIT_ACTIONS} for r in records
        ),
        "verification_latency_seconds": [a["latency_seconds"] for a in verified
                                          if a["latency_seconds"] is not None],
        "unknown_verification_latencies": sum(a["latency_seconds"] is None for a in verified),
    }


def _production_delta(records: list[dict]) -> dict | None:
    before = records[0].get("state", {}).get("factory", {}).get("produced")
    after = records[-1].get("after_state", {}).get("factory", {}).get("produced")
    if not isinstance(before, dict) or not isinstance(after, dict):
        return None
    if any(type(value) not in {int, float} or not math.isfinite(value) or value < 0
           for value in [*before.values(), *after.values()]):
        raise ValueError("Invalid production counter")
    delta = {key: after.get(key, 0) - before.get(key, 0) for key in before.keys() | after.keys()}
    return None if any(value < 0 for value in delta.values()) else delta


def summarize(path: Path) -> dict:
    records = read_records(path)
    last = records[-1]
    calls = [r for r in records if r.get("model_call")]
    observed_victory = any(
        r["after_state"].get("victory") is True
        and r["after_state"].get("victory_source") == "native:base-game-rocket-launch"
        for r in records
    )
    return {
        "session_id": last["session_id"], "world_kind": last["world_kind"],
        "target": last["target"], "policy": last["policy"],
        "evidence_class": "synthetic" if last["world_kind"] == "mock" else "tool-assisted",
        "terminal_status": last["status"], "terminal_reason": last.get("reason"),
        "records": len(records), "model_calls": len(calls),
        **_attempt_counts(records),
        "observed_production_delta": _production_delta(records),
        "milestones": last.get("completed_goals", {}),
        "native_victory_event_observed": observed_victory and last["world_kind"] != "mock",
        "input_tokens": sum((r.get("usage") or {}).get("input_tokens", 0) for r in calls),
        "token_usage_complete": all(
            type((r.get("usage") or {}).get("input_tokens")) is int for r in calls
        ),
        "models": sorted({r["resolved_model"] for r in calls if r.get("resolved_model")}),
    }


def cli() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("logs", nargs="+", type=Path)
    args = parser.parse_args()
    print(json.dumps([summarize(path) for path in args.logs], indent=2, allow_nan=False))


if __name__ == "__main__":
    cli()
