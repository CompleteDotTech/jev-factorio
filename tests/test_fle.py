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


def test_adoption_requires_resume():
    with pytest.raises(ValueError, match="requires resume"):
        FleBackend().start(adopt_session=True)


def test_observation_admits_raw_resources_only_from_fair_native_targets():
    fle = pytest.importorskip("fle.env")
    calls = []

    class Fair:
        def call(self, function, *arguments):
            calls.append((function, arguments))
            if function == "observe":
                return {}
            resource, radius = arguments
            assert function == "next_mine_target" and radius == 128
            return {
                "name": resource,
                "surface_index": 1,
                "position": {"x": 3 if resource == "coal" else 4, "y": 5},
            }

    class Tools:
        namespace = None

        @staticmethod
        def inspect_inventory(*arguments):
            return {}

        @staticmethod
        def get_entities(*arguments):
            return []

        def nearest(self, resource):
            raise AssertionError(f"stale FLE lookup used for {resource}")

    backend = FleBackend()
    backend._fair = Fair()
    backend._instance = type("Instance", (), {
        "namespace": Tools(),
        "rcon_client": type("Rcon", (), {
            "send_command": lambda self, command: (
                '{"tick": 17, "session_id": "native-test", "position": [0, 0]}'
            ),
        })(),
    })()

    observed = backend.observe()

    assert calls == [
        ("observe", ()),
        ("next_mine_target", ("coal", 128)),
        ("next_mine_target", ("iron-ore", 128)),
    ]
    assert backend._resources == {
        "coal": fle.Position(x=3, y=5),
        "iron-ore": fle.Position(x=4, y=5),
    }
    assert observed.nearby_resources == {"coal": pytest.approx(34 ** 0.5),
                                         "iron-ore": pytest.approx(41 ** 0.5)}


@pytest.mark.parametrize("target", [
    {},
    {"name": "coal", "surface_index": 1, "position": {"x": True, "y": 0}},
    {"name": "coal", "surface_index": 1, "position": {"x": float("inf"), "y": 0}},
    {"name": "stone", "surface_index": 1, "position": {"x": 0, "y": 0}},
])
def test_native_mine_target_rejects_missing_or_invalid_native_evidence(target):
    pytest.importorskip("fle.env")
    backend = FleBackend()
    backend._fair = type("Fair", (), {
        "call": lambda self, function, *arguments: target,
    })()

    with pytest.raises(RuntimeError, match="fair native coal target"):
        backend.native_mine_target("coal")


@pytest.mark.parametrize("invalid", ["", "unmarked", "missing", "invalid", "identified"])
def test_adoption_only_identifies_valid_legacy_session(invalid):
    lua = pytest.importorskip("lupa.lua54").LuaRuntime()
    lua.execute(
        "storage = {jev_factorio_session = true}; "
        "jev_fle_runtime = {agent_characters = {{valid = true}}, coal = 7}; "
        "rcon = {print = function(value) output = value end}"
    )
    original = {
        "": "",
        "unmarked": "storage.jev_factorio_session = false",
        "missing": "jev_fle_runtime.agent_characters = nil",
        "invalid": "jev_fle_runtime.agent_characters[1].valid = false",
        "identified": "jev_fle_runtime.jev_session_id = 'existing'",
    }
    lua.execute(original[invalid])

    class Connection:
        def send_command(self, command):
            lua.execute(command.removeprefix("/sc "))
            return lua.eval("output")

    if invalid:
        with pytest.raises(Exception, match="assertion failed"):
            FleBackend._adopt_session(Connection())
        assert lua.eval("jev_fle_runtime.jev_session_id") == (
            "existing" if invalid == "identified" else None
        )
    else:
        identity = FleBackend._adopt_session(Connection())
        assert len(identity) == 32
        assert lua.eval("jev_fle_runtime.jev_session_id") == identity
    assert lua.eval("jev_fle_runtime.coal") == 7
