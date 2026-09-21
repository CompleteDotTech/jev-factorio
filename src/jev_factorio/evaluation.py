"""Summarize evidence logs without relabeling mock runs as live benchmarks."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from .research_events import EvidenceError, load_json
from .research_evaluation import evaluate_run
from .research_reports import experiment_summary, write_report


def _summarize_legacy(path: Path) -> dict:
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


def _is_research(path: Path) -> bool:
    if path.is_dir():
        return True
    with path.open("rb") as stream:
        for line in stream:
            if line.strip():
                first = load_json(line)
                return any(key in first for key in ("schema", "event_type", "event_hash"))
    raise EvidenceError("Empty evaluation log")


def summarize(path: Path, *, manifest_path: Path | None = None,
              allow_mixed_treatments: bool = False) -> dict:
    """Keep the legacy API; event runs additionally require a bound manifest."""
    path = Path(path)
    if _is_research(path):
        return evaluate_run(path, manifest_path=manifest_path,
                            allow_mixed_treatments=allow_mixed_treatments).summary
    if manifest_path or allow_mixed_treatments:
        raise EvidenceError("Research options cannot authenticate a legacy log")
    return _summarize_legacy(path)


def cli(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("logs", nargs="+", type=Path, help="Legacy JSONL, event JSONL, or run directories")
    parser.add_argument("--manifest", type=Path, help="Manifest for a single event input")
    parser.add_argument("--output-dir", type=Path, help="New report directory; existing paths are never overwritten")
    parser.add_argument("--parquet", action="store_true", help="Also export typed Parquet (requires evaluation extra)")
    parser.add_argument("--pair", nargs=2, metavar=("BASELINE", "TREATMENT"), help="Compare matched condition labels")
    parser.add_argument("--allow-mixed-treatments", action="store_true",
                        help="Inspect mixed treatments; never pool them or bypass integrity validation")
    args = parser.parse_args(argv)
    try:
        kinds = [_is_research(path) for path in args.logs]
        if any(kinds) and not all(kinds):
            raise EvidenceError("Do not mix legacy and research event inputs")
        if args.manifest and len(args.logs) != 1:
            raise EvidenceError("--manifest requires exactly one event input")
        if args.parquet and not args.output_dir:
            raise EvidenceError("--parquet requires --output-dir")
        if not any(kinds):
            if args.manifest or args.output_dir or args.parquet or args.pair or args.allow_mixed_treatments:
                raise EvidenceError("Research reports require event inputs; legacy logs remain unauthenticated")
            result = [_summarize_legacy(path) for path in args.logs]
        else:
            evaluations = [evaluate_run(path, manifest_path=args.manifest,
                                       allow_mixed_treatments=args.allow_mixed_treatments) for path in args.logs]
            options = {"pair": tuple(args.pair) if args.pair else None,
                       "allow_mixed_treatments": args.allow_mixed_treatments}
            result = (write_report(evaluations, args.output_dir, parquet=args.parquet, **options)
                      if args.output_dir else experiment_summary(evaluations, **options))
        print(json.dumps(result, indent=2, allow_nan=False))
    except (ValueError, OSError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    cli()
