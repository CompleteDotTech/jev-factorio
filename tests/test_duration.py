from jev_factorio import loop


def test_duration_runs_without_step_limit_and_stops_at_deadline(monkeypatch):
    clock = [0.0]
    decisions = []
    monkeypatch.setattr(loop.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(loop.time, "sleep", lambda delay: clock.__setitem__(0, clock[0] + delay))
    agent = loop.AgentLoop(object(), jev=object(), tick_seconds=2)
    monkeypatch.setattr(agent, "step", lambda: decisions.append(clock[0]))
    agent.run(steps=None, duration_seconds=25)
    assert len(decisions) == 13
    assert clock[0] == 25


def test_duration_checks_deadline_after_slow_step(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(loop.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(loop.time, "sleep", lambda delay: clock.__setitem__(0, clock[0] + delay))
    agent = loop.AgentLoop(object(), jev=object(), tick_seconds=2)
    monkeypatch.setattr(agent, "step", lambda: clock.__setitem__(0, clock[0] + 10))
    agent.run(steps=None, duration_seconds=5)
    assert clock[0] == 10
