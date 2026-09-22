"""Bounded decoded-source verification using the canonical producer contract."""
from __future__ import annotations

from .research_log import (
    INTEGRITY_SCHEMA, MAX_RECORD_BYTES, canonical_bytes, digest,
    validate_event, validate_manifest,
)


def verify_source(
    rows: list[tuple[int, dict]], manifest: dict | None, seal: dict | None,
    expected_head: str | None,
) -> dict:
    """Verify already-bounded input without rereading files or collecting provenance."""
    gaps = []
    manifest_hash = None
    run_id = None
    if manifest is None:
        gaps.append("missing_manifest")
    else:
        validate_manifest(manifest)
        if len(canonical_bytes(manifest)) + 1 > MAX_RECORD_BYTES:
            raise ValueError("Manifest exceeds canonical size limit")
        manifest_hash = digest(manifest)
        run_id = manifest["run_id"]
    previous = anchor = manifest_hash
    last_monotonic = -1
    finished = False
    for count, (_, event) in enumerate(rows, 1):
        validate_event(event)
        if len(canonical_bytes(event)) + 1 > MAX_RECORD_BYTES:
            raise ValueError("Event exceeds canonical size limit")
        if run_id is None:
            run_id = event["run_id"]
        if finished or event["run_id"] != run_id or event["sequence"] != count:
            raise ValueError("Mixed, reordered, duplicate, or post-terminal evidence")
        if count == 1 and previous is None:
            previous = anchor = event["prev_hash"]
        if event["prev_hash"] != previous:
            raise ValueError("Broken evidence hash chain")
        computed = digest({key: value for key, value in event.items() if key != "event_hash"})
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
            payload = event["payload"]
            if set(payload) != {"outcome", "error_type"}:
                raise ValueError("Invalid terminal payload")
            if payload["outcome"] not in ("returned", "error", "interrupted"):
                raise ValueError("Invalid run outcome")
            error_type = payload["error_type"]
            if error_type is not None and (type(error_type) is not str or not error_type):
                raise ValueError("Invalid terminal error type")
            finished = True
        previous, last_monotonic = computed, monotonic
    if not rows:
        gaps.append("missing_run_start")
    if not finished:
        gaps.append("missing_run_finish")
    if seal is None:
        gaps.append("missing_integrity_seal")
    else:
        expected = {
            "schema": INTEGRITY_SCHEMA, "schema_version": 1,
            "run_id": run_id, "manifest_hash": anchor, "event_count": len(rows),
            "final_event_hash": previous,
        }
        if (not finished or type(seal.get("schema_version")) is not int
                or type(seal.get("event_count")) is not int or seal != expected):
            raise ValueError("Run seal does not match evidence")
    if expected_head is not None and expected_head != previous:
        raise ValueError("Trusted final hash does not match evidence")
    return {
        "complete": not gaps, "manifest_hash": manifest_hash,
        "final_event_hash": previous if rows else None,
        "event_count": len(rows), "gaps": gaps,
    }
