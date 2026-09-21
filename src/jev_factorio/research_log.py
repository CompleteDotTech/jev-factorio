"""Small, opt-in durable event sink; controller checkpoints remain independent.

The instrumentation depends only on EventSink.emit, not this file format.
This is not a complete experiment-provenance, replay, or supervisor system.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Protocol
from uuid import uuid4


class ResearchLogError(RuntimeError):
    """A trace cannot be persisted. Never interpret this as a backend failure."""


class EventSink(Protocol):
    def emit(self, event_type: str, payload: dict) -> None:
        """Persist a detached event before returning, or raise on failure."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def canonical_json(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode("utf-8")


def safe_payload(value: object, secrets: Iterable[str] = ()) -> object:
    """Copy JSON data, redact known credentials, and label invalid numbers.

    Never stringify opaque objects or exception bodies. Unknown secrets in free
    text cannot be guaranteed absent; review traces before publishing them.
    """
    secrets = tuple(sorted({s for s in secrets if isinstance(s, str) and s},
                           key=len, reverse=True))
    credential_keys = {"apikey", "apitoken", "accesstoken", "refreshtoken",
                       "authorization", "password", "secret", "cookie", "setcookie",
                       "typesafeapikey", "cloudflareapitoken"}

    def text(item: str) -> str:
        for secret in secrets:
            item = item.replace(secret, "[REDACTED]")
        return re.sub(r"(?i)\bBearer\s+[^\s\"'<>]+", "Bearer [REDACTED]", item)

    def copy(item):
        if item is None or type(item) in (bool, int):
            return item
        if isinstance(item, float):
            return item if math.isfinite(item) else {"invalid_numeric": repr(item)}
        if isinstance(item, str):
            return text(item)
        if isinstance(item, dict):
            return {text(key) if isinstance(key, str) else "[non-string-key]":
                    "[REDACTED]" if isinstance(key, str) and
                    key.lower().replace("_", "").replace("-", "") in credential_keys
                    else copy(child) for key, child in item.items()}
        if isinstance(item, (list, tuple)):
            return [copy(child) for child in item]
        return "[unsupported value]"

    return copy(value)


class ResearchLog:
    """One process, one fresh run directory, fsync on every event.

    A failed write poisons this writer. It never appends to or silently repairs
    an existing run. SHA-256 chaining detects changes relative to a trusted head,
    not adversarial rewriting or deletion of a complete suffix on its own.
    """

    def __init__(self, run_dir: str | Path, *, metadata: dict | None = None,
                 secrets: Iterable[str] = ()):
        self.run_dir = Path(run_dir)
        self.run_id = uuid4().hex
        self._secrets = tuple(secrets)
        self._lock = threading.Lock()
        self._sequence = 0
        self._failed = False
        self._closed = False
        self._stream = None
        manifest = {"schema": "jev-factorio.run.v1", "run_id": self.run_id,
                    "created_utc": utc_now(), "scope": "controller-causal-trace",
                    "directory_fsync": os.name == "posix",
                    "metadata": safe_payload(metadata or {}, self._secrets)}
        try:
            new_directories = []
            directory = self.run_dir.resolve()
            while not directory.exists():
                new_directories.append(directory)
                directory = directory.parent
            self.run_dir.mkdir(parents=True, exist_ok=False)
            data = canonical_json(manifest)
            with (self.run_dir / "manifest.json").open("xb") as stream:
                self._persist(stream, data + b"\n")
            self._previous = hashlib.sha256(data).hexdigest()
            self._stream = (self.run_dir / "events.jsonl").open("xb")
            self.emit("run_started", {"manifest_sha256": self._previous})
            # File fsync alone does not persist newly created directory entries.
            # Windows has no portable directory-fsync API in Python.
            if os.name == "posix":
                for created in [*new_directories, directory]:
                    fd = os.open(created, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
                    try:
                        os.fsync(fd)
                    finally:
                        os.close(fd)
        except Exception:
            self._failed = True
            if self._stream is not None:
                try:
                    self._stream.close()
                except OSError:
                    pass
            raise ResearchLogError("Cannot create a fresh research run") from None

    @staticmethod
    def _persist(stream, data: bytes) -> None:
        if stream.write(data) != len(data):
            raise OSError("Incomplete research write")
        stream.flush()
        os.fsync(stream.fileno())

    def emit(self, event_type: str, payload: dict) -> None:
        with self._lock:
            if self._closed or self._failed:
                raise ResearchLogError("Research writer is closed or failed")
            try:
                if not re.fullmatch(r"[a-z][a-z0-9_]*", event_type):
                    raise ValueError("Invalid event type")
                event = {"schema": "jev-factorio.event.v1", "run_id": self.run_id,
                         "sequence": self._sequence + 1, "event_type": event_type,
                         "utc": utc_now(), "monotonic_ns": time.monotonic_ns(),
                         "payload": safe_payload(payload, self._secrets),
                         "prev_hash": self._previous}
                digest = hashlib.sha256(canonical_json(event)).hexdigest()
                self._persist(self._stream, canonical_json({**event, "event_hash": digest}) + b"\n")
                self._sequence += 1
                self._previous = digest
            except Exception:
                self._failed = True
                raise ResearchLogError("Research event persistence failed") from None

    def close(self, *, outcome: str = "finished") -> None:
        if self._closed:
            return
        try:
            if not self._failed:
                self.emit("run_finished", {"outcome": outcome})
        finally:
            self._closed = True
            try:
                self._stream.close()
            except OSError:
                self._failed = True
                raise ResearchLogError("Cannot close research writer") from None

    def __enter__(self) -> ResearchLog:
        return self

    def __exit__(self, kind, error, traceback) -> None:
        try:
            self.close(outcome="error" if kind else "finished")
        except Exception:
            if kind is None:
                raise


def verify_run(run_dir: str | Path) -> dict:
    """Read-only integrity check. A clean finish is not a gameplay victory."""
    directory = Path(run_dir)
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    if manifest.get("schema") != "jev-factorio.run.v1":
        raise ValueError("Unsupported research manifest")
    previous = hashlib.sha256(canonical_json(manifest)).hexdigest()
    count, last_type = 0, None
    with (directory / "events.jsonl").open("rb") as stream:
        for line in stream:
            if not line.endswith(b"\n"):
                raise ValueError("Incomplete research event")
            event = json.loads(line)
            digest = event.pop("event_hash")
            if (event["schema"] != "jev-factorio.event.v1"
                    or event["run_id"] != manifest["run_id"]
                    or type(event["sequence"]) is not int or event["sequence"] != count + 1
                    or event["prev_hash"] != previous
                    or hashlib.sha256(canonical_json(event)).hexdigest() != digest):
                raise ValueError("Research event integrity mismatch")
            count += 1
            previous, last_type = digest, event["event_type"]
    return {"event_count": count, "head_hash": previous,
            "clean_finish": last_type == "run_finished"}


def validate_output_paths(sink: EventSink | None, *paths: str | Path | None) -> None:
    """Protect the built-in sink from aliased legacy logs and checkpoints."""
    if not isinstance(sink, ResearchLog):
        return
    reserved = [sink.run_dir / name for name in ("manifest.json", "events.jsonl")]
    for value in paths:
        if value is None:
            continue
        path = Path(value)
        if any(path.resolve() == target.resolve() or
               (path.exists() and path.samefile(target)) for target in reserved):
            raise ValueError("Research files must be separate from legacy logs and checkpoints")
