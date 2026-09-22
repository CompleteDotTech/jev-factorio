"""Read-only validation of the proposed v1 research-event interchange contract.

This is deliberately independent of the (separately implemented) event writer.
See docs/RESEARCH_EVALUATION.md for canonical bytes and producer requirements.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import tempfile
from typing import Any

EVENT_SCHEMA = "jev-factorio.event.v1"
HASH_PATTERN = re.compile(r"sha256:[0-9a-f]{64}\Z")
MAX_LINE_BYTES = 16 * 1024 * 1024


class EvidenceError(ValueError):
    """Evidence is malformed, unsupported, inconsistent, or fails integrity."""


class MixedTreatmentError(EvidenceError):
    """A run cannot be attributed to one unchanged treatment."""


def canonical(value: Any) -> bytes:
    """Python-v1 canonical JSON, not RFC 8785; never allow NaN/Infinity."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode("utf-8")


def digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(canonical(value)).hexdigest()


def _object(pairs: list[tuple[str, Any]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise EvidenceError("Duplicate JSON object key")
        result[key] = value
    return result


def _constant(value: str) -> None:
    raise EvidenceError("Non-finite JSON number")


def load_json(data: bytes | str) -> dict:
    try:
        value = json.loads(data.decode("utf-8") if isinstance(data, bytes) else data,
                           object_pairs_hook=_object, parse_constant=_constant)
        if not isinstance(value, dict):
            raise EvidenceError("Expected a JSON object")
        # Also rejects float overflow (e.g. 1e999) and invalid Unicode surrogates.
        canonical(value)
        return value
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise EvidenceError("Invalid JSON evidence") from exc


def nonnegative_int(value: Any, name: str) -> int:
    if type(value) is not int or value < 0 or value > 2**63 - 1:
        raise EvidenceError(f"{name} must be a nonnegative signed-64-bit integer")
    return value


def text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise EvidenceError(f"{name} must be a nonempty string")
    return value


def utc(value: Any) -> datetime:
    text(value, "time.utc")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise EvidenceError("Invalid UTC timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise EvidenceError("Timestamp must explicitly use UTC")
    return parsed


@dataclass(frozen=True)
class VerifiedRun:
    manifest: dict
    events: tuple[dict, ...]
    integrity: dict
    sources: tuple[Path, Path]


def read_run(path: Path, *, manifest_path: Path | None = None) -> VerifiedRun:
    """Validate the entire captured stream before returning any usable evidence.

    No salvage, skipped lines, version guessing, backend calls, or source writes.
    A hash-valid prefix without run_finished is valid but explicitly incomplete.
    """
    path = Path(path)
    events_path = path / "events.jsonl" if path.is_dir() else path
    manifest_path = Path(manifest_path) if manifest_path else events_path.parent / "manifest.json"
    raw_manifest = manifest_path.read_bytes()
    manifest = load_json(raw_manifest)
    if manifest.get("schema") == "jev-factorio.manifest.v1":
        return _read_core_run(events_path, manifest_path, raw_manifest, manifest)
    if "schema" in manifest:
        raise EvidenceError("Unsupported run manifest format")
    if type(manifest.get("schema_version")) is not int or manifest["schema_version"] != 1:
        raise EvidenceError("Unsupported run manifest schema")
    for key in ("run_id", "session_id", "controller", "policy", "target", "backend"):
        text(manifest.get(key), f"manifest.{key}")
    for key in ("git", "world"):
        if not isinstance(manifest.get(key), dict):
            raise EvidenceError(f"manifest.{key} must be an object")
    commit = text(manifest["git"].get("commit"), "git.commit")
    if not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", commit):
        raise EvidenceError("git.commit must be a full immutable Git object ID")
    if type(manifest["git"].get("dirty")) is not bool:
        raise EvidenceError("git.dirty must be a boolean")
    for key in ("experiment_id", "trial_id", "condition", "pair_id", "world_kind",
                "requested_model", "resolved_model"):
        if manifest.get(key) is not None:
            text(manifest[key], f"manifest.{key}")
    if manifest.get("replicate") is not None:
        nonnegative_int(manifest["replicate"], "replicate")
    seed = manifest["world"].get("seed")
    if seed is not None and (type(seed) not in {int, str} or seed == ""):
        raise EvidenceError("World seed must be an integer or nonempty string")
    for container, keys in ((manifest["world"], ("settings_sha256", "initial_save_sha256")),
                            (manifest["git"], ("patch_sha256",))):
        for key in keys:
            value = container.get(key)
            if value is not None and (not isinstance(value, str) or not HASH_PATTERN.fullmatch(value)):
                raise EvidenceError(f"Invalid {key} encoding")
    for key in ("runtime", "configuration", "treatment"):
        if manifest.get(key) is not None and not isinstance(manifest[key], dict):
            raise EvidenceError(f"manifest.{key} must be an object")
    package_hash = (manifest.get("runtime") or {}).get("packages_sha256")
    if package_hash is not None and (not isinstance(package_hash, str) or not HASH_PATTERN.fullmatch(package_hash)):
        raise EvidenceError("Invalid packages_sha256 encoding")
    manifest_hash = digest(manifest)
    events: list[dict] = []
    previous = None
    source_hash = hashlib.sha256()
    with events_path.open("rb") as stream:
        while True:
            raw = stream.readline(MAX_LINE_BYTES + 1)
            if not raw:
                break
            if len(raw) > MAX_LINE_BYTES:
                raise EvidenceError("Event exceeds the 16 MiB line limit")
            if not raw.endswith(b"\n"):
                raise EvidenceError("Unterminated event line; possible torn write")
            source_hash.update(raw)
            event = load_json(raw)
            seq = len(events) + 1
            if event.get("schema") != EVENT_SCHEMA:
                raise EvidenceError(f"Unsupported event schema at sequence {seq}")
            if type(event.get("sequence")) is not int or event["sequence"] != seq:
                raise EvidenceError(f"Noncontiguous event sequence at {seq}")
            if event.get("run_id") != manifest["run_id"]:
                raise EvidenceError("Event run_id differs from manifest")
            text(event.get("segment_id"), "segment_id")
            text(event.get("event_type"), "event_type")
            for key in ("time", "correlation", "payload"):
                if not isinstance(event.get(key), dict):
                    raise EvidenceError(f"Event {key} must be an object")
            utc(event["time"].get("utc"))
            nonnegative_int(event["time"].get("monotonic_ns"), "monotonic_ns")
            if event["time"].get("factorio_tick") is not None:
                nonnegative_int(event["time"]["factorio_tick"], "factorio_tick")
            for key, value in event["correlation"].items():
                if value is not None:
                    text(value, f"correlation.{key}")
            if "prev_hash" not in event or event["prev_hash"] != previous:
                raise EvidenceError(f"Broken previous-hash link at sequence {seq}")
            claimed = event.get("event_hash")
            if not isinstance(claimed, str) or not HASH_PATTERN.fullmatch(claimed):
                raise EvidenceError("Invalid event_hash encoding")
            body = {key: value for key, value in event.items() if key != "event_hash"}
            if digest(body) != claimed:
                raise EvidenceError(f"Event hash mismatch at sequence {seq}")
            if seq == 1:
                if event["event_type"] != "run_started":
                    raise EvidenceError("First event must be run_started")
                if event["payload"].get("manifest_sha256") != manifest_hash:
                    raise EvidenceError("Run manifest is not bound to the event chain")
            elif event["event_type"] == "run_started":
                raise EvidenceError("Duplicate run_started; resumes are not new runs")
            if events and events[-1]["event_type"] == "run_finished":
                raise EvidenceError("Events after run_finished are not permitted")
            events.append(event)
            previous = claimed
    if not events:
        raise EvidenceError("Empty research event stream")
    # Detect ordinary concurrent edits/appends; this is not a malicious-writer lock.
    if manifest_path.read_bytes() != raw_manifest:
        raise EvidenceError("Manifest changed during evaluation")
    with events_path.open("rb") as stream:
        second_hash = hashlib.sha256()
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            second_hash.update(block)
    if second_hash.digest() != source_hash.digest():
        raise EvidenceError("Event stream changed during evaluation; use a captured copy")
    return VerifiedRun(
        manifest, tuple(events), {
            "schema": "jev-factorio.integrity.v1", "valid": True,
            "source_format": "evaluator-proposed-v1",
            "run_id": manifest["run_id"], "event_count": len(events),
            "head_hash": previous, "manifest_sha256": manifest_hash,
            "events_file_sha256": "sha256:" + source_hash.hexdigest(),
            "manifest_file_sha256": "sha256:" + hashlib.sha256(raw_manifest).hexdigest(),
            "complete": events[-1]["event_type"] == "run_finished",
            "authenticated": False,
            "limitation": "An unanchored chain cannot detect a rehashed rewrite or all valid-prefix truncations.",
        }, (manifest_path.resolve(), events_path.resolve()),
    )


def _read_core_run(events_path: Path, manifest_path: Path, raw_manifest: bytes,
                   manifest: dict) -> VerifiedRun:
    from .research_log import verify_run

    if (events_path.name != "events.jsonl" or manifest_path.name != "manifest.json"
            or events_path.parent.resolve() != manifest_path.parent.resolve()):
        raise EvidenceError("Core evidence requires its original run-directory layout")
    seal_path = events_path.parent / "integrity.json"
    raw_seal = seal_path.read_bytes() if seal_path.exists() else None
    events = []
    source_hash = hashlib.sha256()
    with tempfile.TemporaryDirectory(prefix="jev-evaluation-") as temporary:
        captured = Path(temporary)
        (captured / "manifest.json").write_bytes(raw_manifest)
        if raw_seal is not None:
            (captured / "integrity.json").write_bytes(raw_seal)
        with events_path.open("rb") as stream, (captured / "events.jsonl").open("wb") as copy:
            for raw in iter(lambda: stream.readline(MAX_LINE_BYTES + 1), b""):
                if len(raw) > MAX_LINE_BYTES or not raw.endswith(b"\n"):
                    raise EvidenceError("Oversized or unterminated core event")
                copy.write(raw)
                source_hash.update(raw)
                events.append(load_json(raw))
        try:
            verified = verify_run(captured, allow_incomplete=True)
        except (ValueError, OSError) as exc:
            raise EvidenceError(f"Core evidence verification failed: {exc}") from exc
    second_hash = hashlib.sha256()
    with events_path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            second_hash.update(block)
    if (manifest_path.read_bytes() != raw_manifest
            or second_hash.digest() != source_hash.digest()
            or (seal_path.read_bytes() if seal_path.exists() else None) != raw_seal):
        raise EvidenceError("Core evidence changed during evaluation; use a captured copy")
    if not events:
        raise EvidenceError("Empty core event stream")
    return VerifiedRun(manifest, tuple(events), {
        "schema": "jev-factorio.integrity.v1", "valid": True,
        "source_format": "logging-core-v1", "source_manifest_schema": manifest["schema"],
        "source_event_schema": events[0]["schema"], "authenticated": False,
        "run_id": verified["run_id"], "event_count": verified["event_count"],
        "complete": verified["complete"], "head_hash": verified["final_event_hash"],
        "manifest_sha256": verified["manifest_hash"],
        "events_file_sha256": "sha256:" + source_hash.hexdigest(),
        "manifest_file_sha256": "sha256:" + hashlib.sha256(raw_manifest).hexdigest(),
        "seal_file_sha256": ("sha256:" + hashlib.sha256(raw_seal).hexdigest()
                             if raw_seal is not None else None),
        "limitation": "An unanchored chain and seal do not authenticate the producer.",
    }, (manifest_path.resolve(), events_path.resolve()))
