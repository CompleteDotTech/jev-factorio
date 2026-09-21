import pytest

from jev_factorio.backends.fle import FleBackend, SessionRcon


def test_session_commands_isolate_executable_state():
    class Connection:
        def send_command(self, command):
            return command

        def send_commands(self, commands):
            return commands

    client = SessionRcon(Connection())
    assert client.send_command("/sc storage.actions = {}") == (
        "/sc local storage = jev_fle_runtime; storage.actions = {}"
    )
    assert client.send_commands({"init": "/c storage.fast = true", "save": "/save"}) == {
        "init": "/c local storage = jev_fle_runtime; storage.fast = true",
        "save": "/save",
    }


@pytest.mark.parametrize("prefix", ["/sc ", "/c ", "/silent-command ", "/command "])
def test_session_scope_keeps_functions_out_of_saved_storage(prefix):
    lua = pytest.importorskip("lupa.lua54").LuaRuntime()
    lua.execute("storage = {jev_factorio_session = true}; jev_fle_runtime = {}")
    command = SessionRcon.scoped(
        prefix + "storage.action = function() storage.count = 1 end"
    )
    lua.execute(command[len(prefix):])
    lua.execute("jev_fle_runtime.action()")
    assert lua.eval("storage.jev_factorio_session") is True
    assert lua.eval("storage.action") is None
    assert lua.eval("storage.count") is None
    assert lua.eval("jev_fle_runtime.count") == 1


def test_fle_refuses_to_reset_an_unmarked_world(monkeypatch):
    pytest.importorskip("fle.env")
    import factorio_rcon

    connections = []

    class UnmarkedConnection:
        def __init__(self, *args, **kwargs):
            self.commands = []
            self.closed = False
            connections.append(self)

        def connect(self):
            pass

        def send_command(self, command):
            self.commands.append(command)
            return "false\n"

        def close(self):
            self.closed = True

    monkeypatch.setenv("FACTORIO_RCON_PASSWORD", "test-only-password")
    monkeypatch.setattr(factorio_rcon, "RCONClient", UnmarkedConnection)
    with pytest.raises(RuntimeError, match="unmarked world"):
        FleBackend().start()
    assert connections[0].closed
    assert connections[0].commands == [
        "/sc rcon.print(storage.jev_factorio_session == true)"
    ]


def test_resume_refuses_missing_session_without_reset(monkeypatch):
    pytest.importorskip("fle.env")
    import factorio_rcon

    class MissingSession:
        def __init__(self, *args, **kwargs):
            self.commands = []
            self.closed = False

        def send_command(self, command):
            self.commands.append(command)
            return "true" if len(self.commands) == 1 else "false"

        def close(self):
            self.closed = True

    client = MissingSession()
    monkeypatch.setenv("FACTORIO_RCON_PASSWORD", "test-only-password")
    monkeypatch.setattr(factorio_rcon, "RCONClient", lambda *args, **kwargs: client)
    with pytest.raises(RuntimeError, match="No live agent session"):
        FleBackend().start(resume=True)
    assert client.closed
    assert len(client.commands) == 2
    assert all(command.startswith("/sc rcon.print(") for command in client.commands)
