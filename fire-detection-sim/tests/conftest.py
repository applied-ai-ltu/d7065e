"""Shared fixtures."""

import time

import pytest

from fds.agent import agent as agent_mod
from fds.agent.agent import FireAgent
from tests.agent_fakes import FakeBuildSim, FakeEvac, FakeMQTT, FakeSpread


@pytest.fixture
def agent(monkeypatch):
    monkeypatch.setattr(agent_mod.mqtt, "Client", FakeMQTT)
    a = FireAgent(
        bs=FakeBuildSim(),
        spread_predictor=FakeSpread(),
        evacuation_planner=FakeEvac(),
        db_config=None,
    )
    a.room_levels = {
        "A1": "level0",
        "A2": "level0",
        "A3": "level0",
        "EXIT": "level0",
        "B1": "level1",
    }
    a._door_ids = {lv: [f"{lv}-D1-door", f"{lv}-D2-door"] for lv in a.levels}
    a._start_ts = time.time()
    return a
