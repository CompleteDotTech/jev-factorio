"""Integration against real repository controllers, with explicit offline backends."""
import json
import sys
from pathlib import Path

import pytest
import requests

from jev_factorio import main
from jev_factorio import research_log as rl
from jev_factorio.backends.mock import MockBackend


@pytest.fixture(autouse=True)
def offline(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for key in ("TYPESAFE_API_KEY", "CLOUDFLARE_API_TOKEN", "CLOUDFLARE_ACCOUNT_ID",
                "JEV_RUN_DIR", "JEV_LOG_FILE", "JEV_TICK_SECONDS", "JEV_CONFIDENCE_FLOOR", "JEV_BACKEND"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(requests, "post", lambda *a, **kw: pytest.fail("Unexpected provider call"))


def invoke(monkeypatch, *arguments):
    monkeypatch.setattr(sys, "argv", ["jev-factorio", *map(str, arguments)])
    main.cli()


class CountingBackend(MockBackend):
    def __init__(self):
        super().__init__()
        self.session_id = "mock:fixed-test-world"
        self.observations = 0
        self.actions = []

    def observe(self):
        self.observations += 1
        return super().observe()

    def act(self, action):
        self.actions.append(action)
        return super().act(action)


@pytest.mark.parametrize("controller", ["flat", "hierarchical"])
def test_research_logging_preserves_gameplay_and_legacy_records(tmp_path, monkeypatch, controller):
    worlds = []
    logs = []
    for enabled in (False, True):
        backend = CountingBackend()
        worlds.append(backend)
        monkeypatch.setattr(main, "make_backend", lambda *a, **kw: backend)
        legacy = tmp_path / f"legacy-{enabled}.jsonl"
        logs.append(legacy)
        arguments = ["--backend", "mock", "--controller", controller,
                     "--steps", "40", "--tick-seconds", "0", "--log-file", str(legacy)]
        if controller == "hierarchical":
            arguments += ["--mock-model", "--target", "bootstrap_mining"]
        if enabled:
            arguments += ["--run-dir", str(tmp_path / "research")]
        invoke(monkeypatch, *arguments)
    assert worlds[0].actions == worlds[1].actions
    assert worlds[0].observations == worlds[1].observations
    assert worlds[0].observe() == worlds[1].observe()
    old, new = [[json.loads(line) for line in path.read_text().splitlines()] for path in logs]
    assert len(old) == len(new)
    for first, second in zip(old, new):
        assert set(first) == set(second)
        for key in ("action", "outcome", "state", "after_state", "status", "verified", "completed_goals"):
            assert first.get(key) == second.get(key)
    if controller == "flat":
        assert logs[0].read_bytes() == logs[1].read_bytes()
    else:
        from jev_factorio.evaluation import summarize
        assert summarize(logs[1])["evidence_class"] == "synthetic"
    assert rl.verify_run(tmp_path / "research")["event_count"] == 4


@pytest.mark.parametrize("with_legacy", [False, True])
def test_new_run_directory_and_optional_legacy_file(tmp_path, monkeypatch, with_legacy):
    run = tmp_path / "run with spaces"
    arguments = ["--backend", "mock", "--steps", "2", "--tick-seconds", "0", "--run-dir", run]
    if with_legacy:
        arguments += ["--log-file", run / "decisions.jsonl"]
    invoke(monkeypatch, *arguments)
    assert (run / "decisions.jsonl").exists() is with_legacy
    result = rl.verify_run(run)
    assert result["complete"] is True and result["outcome"] == "returned"
    event_types = [json.loads(line)["event_type"] for line in (run / "events.jsonl").read_text().splitlines()]
    assert event_types == ["run_started", "controller_initialized", "controller_stopped", "run_finished"]
    manifest = json.loads((run / "manifest.json").read_bytes())
    assert manifest["configuration"]["legacy_log_enabled"] is with_legacy
    assert manifest["configuration"]["steps"] == 2
    assert str(tmp_path) not in json.dumps(manifest)


def test_logging_remains_opt_in(tmp_path, monkeypatch):
    def unexpected(*args, **kwargs):
        pytest.fail("Research logger initialized when disabled")
    monkeypatch.setattr(main, "ResearchLog", unexpected)
    invoke(monkeypatch, "--backend", "mock", "--steps", "0")
    assert list(tmp_path.iterdir()) == []


def test_environment_and_explicit_cli_precedence(tmp_path, monkeypatch):
    dotenv = tmp_path / ".env"
    dotenv.write_text(f"JEV_RUN_DIR={tmp_path / 'dotenv-run'}\n")
    monkeypatch.setenv("JEV_RUN_DIR", str(tmp_path / "exported-run"))
    invoke(monkeypatch, "--backend", "mock", "--steps", "0", "--run-dir", tmp_path / "cli-run")
    assert rl.verify_run(tmp_path / "cli-run")["complete"] is True
    assert not (tmp_path / "exported-run").exists()
    invoke(monkeypatch, "--backend", "mock", "--steps", "0")
    assert rl.verify_run(tmp_path / "exported-run")["complete"] is True
    monkeypatch.delenv("JEV_RUN_DIR")
    invoke(monkeypatch, "--backend", "mock", "--steps", "0")
    assert rl.verify_run(tmp_path / "dotenv-run")["complete"] is True


@pytest.mark.parametrize("failure", ["existing", "fsync"])
def test_logging_startup_failure_precedes_backend_initialization(tmp_path, monkeypatch, failure):
    run = tmp_path / "run"
    if failure == "existing":
        run.mkdir()
    else:
        monkeypatch.setattr(rl.os, "fsync", lambda fd: (_ for _ in ()).throw(OSError("disk")))
    monkeypatch.setattr(main, "make_backend", lambda *a, **kw: pytest.fail("Backend started"))
    with pytest.raises(SystemExit) as error:
        invoke(monkeypatch, "--backend", "mock", "--steps", "0", "--run-dir", run)
    assert error.value.code == 2


@pytest.mark.parametrize("reserved", ["manifest.json", "events.jsonl", "integrity.json",
                                      "events.jsonl/child", "."])
@pytest.mark.parametrize("argument", ["--log-file", "--checkpoint"])
def test_artifact_aliases_rejected_before_backend(tmp_path, monkeypatch, reserved, argument):
    run = tmp_path / "run"
    monkeypatch.setattr(main, "make_backend", lambda *a, **kw: pytest.fail("Backend started"))
    arguments = ["--backend", "mock", "--steps", "0", "--run-dir", run, argument, run / reserved]
    if argument == "--checkpoint":
        arguments += ["--controller", "hierarchical", "--mock-model"]
    with pytest.raises(SystemExit) as error:
        invoke(monkeypatch, *arguments)
    assert error.value.code == 2
    assert not run.exists()


def test_symlink_alias_to_internal_artifact_is_rejected(tmp_path, monkeypatch):
    run = tmp_path / "run"
    alias = tmp_path / "alias"
    alias.symlink_to(run / "events.jsonl")
    monkeypatch.setattr(main, "make_backend", lambda *a, **kw: pytest.fail("Backend started"))
    with pytest.raises(SystemExit) as error:
        invoke(monkeypatch, "--backend", "mock", "--steps", "0", "--run-dir", run, "--log-file", alias)
    assert error.value.code == 2
    assert not run.exists()


def test_invalid_cli_creates_no_research_artifacts(tmp_path, monkeypatch):
    with pytest.raises(SystemExit):
        invoke(monkeypatch, "--run-dir", tmp_path / "run", "--steps", "-1")
    assert not (tmp_path / "run").exists()


def test_duration_configuration_without_waiting(tmp_path, monkeypatch):
    captured = {}

    class Loop:
        def __init__(self, backend, **options):
            pass
        def run(self, **limits):
            captured.update(limits)

    monkeypatch.setattr(main, "AgentLoop", Loop)
    invoke(monkeypatch, "--backend", "mock", "--duration-hours", "0.01", "--run-dir", tmp_path / "run")
    assert captured == {"steps": None, "duration_seconds": 36.0}
    manifest = json.loads((tmp_path / "run" / "manifest.json").read_text())
    assert manifest["configuration"]["steps"] is None
    assert manifest["configuration"]["duration_seconds"] == 36.0


@pytest.mark.parametrize("exception,outcome", [(RuntimeError("unlogged-private-detail"), "error"),
                                                (KeyboardInterrupt(), "interrupted")])
def test_controller_exception_is_recorded_without_raw_message(tmp_path, monkeypatch, exception, outcome):
    class Loop:
        def __init__(self, backend, **options):
            pass
        def run(self, **limits):
            raise exception

    monkeypatch.setattr(main, "AgentLoop", Loop)
    with pytest.raises(type(exception)):
        invoke(monkeypatch, "--backend", "mock", "--steps", "1", "--run-dir", tmp_path / "run")
    assert rl.verify_run(tmp_path / "run")["outcome"] == outcome
    assert "unlogged-private-detail" not in (tmp_path / "run" / "events.jsonl").read_text()


def test_event_write_failure_stops_before_gameplay_steps(tmp_path, monkeypatch):
    backend = CountingBackend()
    monkeypatch.setattr(main, "make_backend", lambda *a, **kw: backend)
    original = rl._write_durable

    def fail_initialization_event(stream, data):
        if b'"event_type":"controller_initialized"' in data:
            raise OSError("disk failure")
        original(stream, data)

    monkeypatch.setattr(rl, "_write_durable", fail_initialization_event)
    with pytest.raises(OSError):
        invoke(monkeypatch, "--backend", "mock", "--steps", "2", "--run-dir", tmp_path / "run")
    assert backend.actions == [] and backend.observations == 0
    assert not (tmp_path / "run" / "integrity.json").exists()
    assert rl.verify_run(tmp_path / "run", allow_incomplete=True)["complete"] is False
