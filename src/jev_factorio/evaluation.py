"""Summarize evidence logs without relabeling mock runs as live benchmarks."""
from __future__ import annotations

import argparse
import json
from pathlib import Path


def summarize(path: Path) -> dict:
    records = []
    with path.open(encoding="utf-8") as stream:
        for line in stream:
            if line.strip():
                records.append(json.loads(line))
    if not records:
        raise ValueError("Empty evaluation log")
    identity = {(r.get("session_id"), r.get("world_kind"), r.get("target"), r.get("policy"), r.get("requested_model"))
                for r in records}
    if len(identity) != 1 or any(r.get("controller") != "hierarchical" for r in records):
        raise ValueError("Do not mix sessions, policies, targets, or log schemas")
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
        "verified_actions": sum(r.get("verified", False) and r["action"] not in {"observe", "verify"}
                                for r in records),
        "milestones": last.get("completed_goals", {}),
        "native_victory_event_observed": observed_victory and last["world_kind"] != "mock",
        "input_tokens": sum((r.get("usage") or {}).get("input_tokens", 0) for r in calls),
        "token_usage_complete": all(r.get("usage") is not None for r in calls),
        "models": sorted({r["resolved_model"] for r in calls if r.get("resolved_model")}),
    }


def cli() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("logs", nargs="+", type=Path)
    args = parser.parse_args()
    print(json.dumps([summarize(path) for path in args.logs], indent=2))


if __name__ == "__main__":
    cli()
