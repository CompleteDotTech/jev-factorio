import sys

import pytest

from jev_factorio import main
from jev_factorio.jev_client import JevClient, MockJevClient, make_client


@pytest.mark.parametrize(
    ("exported_key", "expected_key"),
    [(None, "test-file-key"), ("test-exported-key", "test-exported-key"), ("", "")],
)
def test_cli_loads_dotenv_without_overriding_environment(
    tmp_path, monkeypatch, exported_key, expected_key
):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("TYPESAFE_API_KEY=test-file-key\nJEV_TICK_SECONDS=3\n")
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    monkeypatch.delenv("CLOUDFLARE_API_TOKEN", raising=False)
    monkeypatch.delenv("JEV_TICK_SECONDS", raising=False)
    if exported_key is not None:
        monkeypatch.setenv("TYPESAFE_API_KEY", exported_key)
    monkeypatch.setattr(sys, "argv", ["jev-factorio", "--backend", "mock", "--steps", "0"])
    captured = {}

    class RecordingLoop:
        def __init__(self, backend, **options):
            captured["client"] = make_client()
            captured["tick_seconds"] = options["tick_seconds"]

        def run(self, steps):
            captured["steps"] = steps

    monkeypatch.setattr(main, "AgentLoop", RecordingLoop)
    main.cli()

    if expected_key:
        assert isinstance(captured["client"], JevClient)
        assert captured["client"].api_key == expected_key
    else:
        assert isinstance(captured["client"], MockJevClient)
    assert captured["tick_seconds"] == 3
    assert captured["steps"] == 0


@pytest.mark.parametrize("arguments", [
    ["--backend", "mock"],
    ["--backend", "fle", "--resume"],
    ["--backend", "fle", "--controller", "hierarchical"],
    ["--backend", "fle", "--controller", "hierarchical", "--resume", "--resume-controller"],
])
def test_adoption_invalid_modes_fail_before_backend(monkeypatch, tmp_path, arguments):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["jev-factorio", "--adopt-session", *arguments])
    monkeypatch.setattr(main, "make_backend", lambda *args, **kwargs: pytest.fail("Backend started"))
    with pytest.raises(SystemExit) as error:
        main.cli()
    assert error.value.code == 2
