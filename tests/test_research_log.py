import json

import pytest

from jev_factorio.research_log import ResearchLog, ResearchLogError, verify_run


def test_envelope_sequence_manifest_and_integrity(tmp_path):
    with ResearchLog(tmp_path / "run", metadata={"controller": "flat"}) as log:
        log.emit("test", {"value": 1})
        log.emit("test", {"value": 2})
    directory = tmp_path / "run"
    records = [json.loads(line) for line in (directory / "events.jsonl").read_text().splitlines()]
    assert [r["sequence"] for r in records] == [1, 2, 3, 4]
    assert all(r["schema"] == "jev-factorio.event.v1" for r in records)
    assert all(r["utc"].endswith("+00:00") for r in records)
    assert all(type(r["monotonic_ns"]) is int for r in records)
    assert len({r["run_id"] for r in records}) == 1
    assert verify_run(directory)["event_count"] == 4
    assert verify_run(directory)["clean_finish"] is True


@pytest.mark.parametrize("corruption", ["edit", "reorder", "partial_line", "remove_middle"])
def test_integrity_rejects_corruption(tmp_path, corruption):
    directory = tmp_path / "run"
    with ResearchLog(directory) as log:
        log.emit("test", {"value": 1})
        log.emit("test", {"value": 2})
    path = directory / "events.jsonl"
    lines = path.read_bytes().splitlines(keepends=True)
    if corruption == "edit":
        lines[1] = lines[1].replace(b'"value":1', b'"value":9')
    elif corruption == "reorder":
        lines[1], lines[2] = lines[2], lines[1]
    elif corruption == "remove_middle":
        del lines[1]
    else:
        lines[-1] = lines[-1][:-2]
    path.write_bytes(b"".join(lines))
    with pytest.raises((ValueError, KeyError)):
        verify_run(directory)


def test_whole_suffix_removal_is_not_claimed_as_detectable(tmp_path):
    directory = tmp_path / "run"
    with ResearchLog(directory) as log:
        log.emit("test", {})
    path = directory / "events.jsonl"
    lines = path.read_bytes().splitlines(keepends=True)
    path.write_bytes(b"".join(lines[:-1]))
    assert verify_run(directory)["clean_finish"] is False


def test_failed_write_poisoned_and_no_reopen_or_repair(tmp_path, monkeypatch):
    directory = tmp_path / "run"
    log = ResearchLog(directory)
    original = log._persist

    def fail(stream, data):
        raise OSError("private secret path")

    monkeypatch.setattr(log, "_persist", fail)
    with pytest.raises(ResearchLogError, match="persistence failed"):
        log.emit("test", {})
    monkeypatch.setattr(log, "_persist", original)
    with pytest.raises(ResearchLogError, match="failed"):
        log.emit("test", {})
    log.close()
    before = (directory / "events.jsonl").read_bytes()
    with pytest.raises(ResearchLogError):
        ResearchLog(directory)
    assert before == (directory / "events.jsonl").read_bytes()
    assert not verify_run(directory)["clean_finish"]


def test_fsync_occurs_before_emit_returns(tmp_path, monkeypatch):
    import jev_factorio.research_log as module
    calls = []
    real = module.os.fsync

    def fsync(fd):
        calls.append(fd)
        return real(fd)

    monkeypatch.setattr(module.os, "fsync", fsync)
    with ResearchLog(tmp_path / "run") as log:
        count = len(calls)
        log.emit("test", {})
        assert len(calls) == count + 1


def test_redaction_invalid_numbers_and_detached_data(tmp_path):
    metadata = {"api_key": "never-persist", "note": "known-secret"}
    with ResearchLog(tmp_path / "run", metadata=metadata, secrets=("known-secret",)) as log:
        data = {"nested": {"Authorization": "Bearer never-persist", "value": "known-secret"},
                "number": float("nan")}
        log.emit("test", data)
        assert data["nested"]["value"] == "known-secret"
    text = (tmp_path / "run/events.jsonl").read_text()
    assert "known-secret" not in text and "never-persist" not in text
    assert "invalid_numeric" in text
    assert "known-secret" not in (tmp_path / "run/manifest.json").read_text()
