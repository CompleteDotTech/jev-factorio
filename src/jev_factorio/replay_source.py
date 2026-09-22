"""Read-only verification of decoded research logging V1 evidence."""
from __future__ import annotations

import hashlib
import json
import math
import re
import uuid
from datetime import datetime


def _keys(value: object, expected: set[str]) -> None:
    if type(value) is not dict or set(value) != expected:
        raise ValueError("Unexpected evidence schema fields")


def _integer(value: object, minimum: int = 0) -> None:
    if type(value) is not int or value < minimum:
        raise ValueError("Invalid evidence integer")


def _hash(value: object) -> None:
    if type(value) is not str or not re.fullmatch(r"sha256:[0-9a-f]{64}", value):
        raise ValueError("Invalid SHA-256 reference")


def _identity(value: object) -> None:
    if type(value) is not str:
        raise ValueError("Invalid run identity")
    try:
        if str(uuid.UUID(value)) != value:
            raise ValueError("Invalid run identity")
    except (ValueError, AttributeError) as error:
        raise ValueError("Invalid run identity") from error


def _utc(value: object) -> None:
    if type(value) is not str or not re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z", value
    ):
        raise ValueError("Invalid UTC timestamp")
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError("Invalid UTC timestamp") from error


def _optional_text(value: object) -> None:
    if value is not None and (type(value) is not str or not value):
        raise ValueError("Invalid optional evidence text")


def _json_value(value: object, depth: int = 0) -> None:
    if depth > 128:
        raise ValueError("Evidence nesting exceeds verification limit")
    if value is None or type(value) in (str, bool, int):
        return
    if type(value) is float and math.isfinite(value):
        return
    if type(value) is list:
        for item in value:
            _json_value(item, depth + 1)
        return
    if type(value) is dict and all(type(key) is str for key in value):
        for item in value.values():
            _json_value(item, depth + 1)
        return
    raise ValueError("Evidence must contain finite JSON values and string keys")


def _digest(value: object) -> str:
    _json_value(value)
    try:
        encoded = json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
            allow_nan=False,
        ).encode("ascii")
    except (ValueError, RecursionError, OverflowError) as error:
        raise ValueError("Invalid canonical evidence") from error
    if len(encoded) + 1 > 1_048_576:
        raise ValueError("Evidence record exceeds V1 size limit")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _configuration(value: object) -> None:
    required = {
        "backend", "controller", "policy", "target", "requested_model", "steps",
        "duration_seconds", "tick_seconds", "confidence_floor", "resume",
        "resume_controller", "adopt_session", "mock_model", "legacy_log_enabled",
        "checkpoint_enabled",
    }
    treatments = {"factory_scheduling", "background_work", "furnace_output_buffers",
                  "furnace_input_belts"}
    if type(value) is not dict or not required <= set(value) <= required | treatments:
        raise ValueError("Unexpected evidence schema fields")
    if value.get("factory_scheduling", "serial") not in ("serial", "ready-work"):
        raise ValueError("Invalid factory scheduling policy")
    if any(type(value.get(key, False)) is not bool for key in treatments - {"factory_scheduling"}):
        raise ValueError("Invalid run treatment flag")
    for key in ("backend", "controller", "policy"):
        if type(value[key]) is not str or not value[key]:
            raise ValueError("Invalid run configuration label")
    for key in ("target", "requested_model"):
        _optional_text(value[key])
    if value["steps"] is not None:
        _integer(value["steps"])
    for key in ("duration_seconds", "tick_seconds", "confidence_floor"):
        number = value[key]
        if key == "duration_seconds" and number is None:
            continue
        if type(number) not in (int, float) or number < 0:
            raise ValueError("Invalid run configuration number")
        if type(number) is float and not math.isfinite(number):
            raise ValueError("Invalid run configuration number")
        if (key == "duration_seconds" and number == 0) or (
            key == "confidence_floor" and number > 1
        ):
            raise ValueError("Run configuration number outside bounds")
    if value["steps"] is not None and value["duration_seconds"] is not None:
        raise ValueError("Run configuration has conflicting limits")
    for key in (
        "resume", "resume_controller", "adopt_session", "mock_model",
        "legacy_log_enabled", "checkpoint_enabled",
    ):
        if type(value[key]) is not bool:
            raise ValueError("Invalid run configuration flag")


def _manifest(value: object) -> None:
    _keys(value, {
        "schema", "schema_version", "run_id", "created_utc", "configuration",
        "provenance", "durability",
    })
    _schema(value, "manifest")
    _identity(value["run_id"])
    _utc(value["created_utc"])
    _configuration(value["configuration"])
    provenance = value["provenance"]
    _keys(provenance, {"git", "runtime", "provider_configuration"})
    _keys(provenance["git"], {"commit", "dirty"})
    commit = provenance["git"]["commit"]
    if commit is not None and (
        type(commit) is not str or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", commit)
    ):
        raise ValueError("Invalid provenance commit")
    dirty = provenance["git"]["dirty"]
    if dirty is not None and type(dirty) is not bool:
        raise ValueError("Invalid provenance dirty flag")
    runtime = provenance["runtime"]
    _keys(runtime, {"python", "system", "machine", "packages"})
    if any(type(runtime[key]) is not str for key in ("python", "system", "machine")):
        raise ValueError("Invalid runtime provenance")
    _keys(runtime["packages"], {
        "jev-factorio", "requests", "python-dotenv",
        "factorio-learning-environment", "a2a-sdk",
    })
    for version in runtime["packages"].values():
        _optional_text(version)
    _keys(provenance["provider_configuration"], {"typesafe", "cloudflare", "factorio_rcon"})
    if any(type(flag) is not bool for flag in provenance["provider_configuration"].values()):
        raise ValueError("Invalid provider presence flag")
    if value["durability"] not in ("file-and-directory-fsync", "file-fsync-only"):
        raise ValueError("Invalid manifest durability")


def _schema(value: dict, kind: str) -> None:
    if value["schema"] != f"jev-factorio.{kind}.v1" or (
        type(value["schema_version"]) is not int or value["schema_version"] != 1
    ):
        raise ValueError("Unsupported evidence schema")


def _event(value: object) -> None:
    _keys(value, {
        "schema", "schema_version", "run_id", "sequence", "event_type", "time",
        "session_id", "correlation", "payload", "prev_hash", "event_hash",
    })
    _schema(value, "event")
    _identity(value["run_id"])
    _integer(value["sequence"], 1)
    if type(value["event_type"]) is not str or not re.fullmatch(
        r"[a-z][a-z0-9_]{0,63}", value["event_type"]
    ):
        raise ValueError("Invalid event type")
    _keys(value["time"], {"utc", "monotonic_ns", "factorio_tick"})
    _utc(value["time"]["utc"])
    _integer(value["time"]["monotonic_ns"])
    if value["time"]["factorio_tick"] is not None:
        _integer(value["time"]["factorio_tick"])
    _optional_text(value["session_id"])
    correlation = value["correlation"]
    if type(correlation) is not dict or not set(correlation) <= {
        "decision_id", "model_call_id", "plan_id", "action_id",
    }:
        raise ValueError("Invalid correlation fields")
    if any(type(item) is not str or not item for item in correlation.values()):
        raise ValueError("Invalid correlation identity")
    if type(value["payload"]) is not dict:
        raise ValueError("Event payload must be an object")
    _hash(value["prev_hash"])
    _hash(value["event_hash"])
    _digest(value)


def verify_source(
    rows: list[tuple[int, dict]], manifest: dict | None, seal: dict | None,
    expected_head: str | None,
) -> dict:
    """Verify source structure and chain; absent evidence remains an explicit gap."""
    gaps = []
    manifest_hash = None
    run_id = None
    if manifest is None:
        gaps.append("missing_manifest")
    else:
        _manifest(manifest)
        manifest_hash = _digest(manifest)
        run_id = manifest["run_id"]
    previous = manifest_hash
    anchor = manifest_hash
    last_monotonic = -1
    finished = False
    for count, (line, event) in enumerate(rows, 1):
        _integer(line, 1)
        _event(event)
        if run_id is None:
            run_id = event["run_id"]
        if finished or event["run_id"] != run_id or event["sequence"] != count:
            raise ValueError("Mixed, reordered, duplicate, or post-terminal evidence")
        if count == 1 and previous is None:
            previous = event["prev_hash"]
            anchor = previous
        if event["prev_hash"] != previous:
            raise ValueError("Broken evidence hash chain")
        computed = _digest({key: value for key, value in event.items() if key != "event_hash"})
        if computed != event["event_hash"]:
            raise ValueError("Evidence hash mismatch")
        monotonic = event["time"]["monotonic_ns"]
        if monotonic < last_monotonic:
            raise ValueError("Monotonic evidence time regressed")
        if count == 1:
            if event["event_type"] != "run_started" or event["payload"] != {"manifest_hash": anchor}:
                raise ValueError("Missing manifest-bound run start")
        elif event["event_type"] == "run_started":
            raise ValueError("Duplicate run start")
        if event["event_type"] == "run_finished":
            _keys(event["payload"], {"outcome", "error_type"})
            outcome = event["payload"]["outcome"]
            _optional_text(event["payload"]["error_type"])
            if type(outcome) is not str or outcome not in {"returned", "error", "interrupted"}:
                raise ValueError("Invalid run outcome")
            finished = True
        previous = computed
        last_monotonic = monotonic
    if not rows:
        gaps.append("missing_run_start")
    if not finished:
        gaps.append("missing_run_finish")
    if seal is None:
        gaps.append("missing_integrity_seal")
    else:
        _keys(seal, {
            "schema", "schema_version", "run_id", "manifest_hash",
            "event_count", "final_event_hash",
        })
        _schema(seal, "integrity")
        _identity(seal["run_id"])
        _integer(seal["event_count"], 1)
        _hash(seal["manifest_hash"])
        _hash(seal["final_event_hash"])
        _digest(seal)
        if not finished or seal != {
            "schema": "jev-factorio.integrity.v1", "schema_version": 1,
            "run_id": run_id, "manifest_hash": anchor, "event_count": len(rows),
            "final_event_hash": previous,
        }:
            raise ValueError("Run seal does not match evidence")
    if expected_head is not None:
        _hash(expected_head)
        if expected_head != previous:
            raise ValueError("Trusted final hash does not match evidence")
    return {
        "complete": not gaps, "manifest_hash": manifest_hash,
        "final_event_hash": previous if rows else None,
        "event_count": len(rows), "gaps": gaps,
    }
