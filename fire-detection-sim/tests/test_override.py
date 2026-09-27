"""Tier-2 server agent: alert dedupe, LLM override policy, watch/stand-down, coordination.

Runs offline: MQTT, BuildSim, LLM and the database are faked (see agent_fakes.py).
"""

import time

from fds.agent import agent as agent_mod
from tests.agent_fakes import alert, published, set_llm

FIRE = dict(severity="confirmed", decision="fire", smoke=15.0, co=30.0)


async def test_confirmed_fire_coordinates_other_floors_once(agent):
    prompts = set_llm(agent, True, 0.9)
    await agent.process_alert(alert("A1", **FIRE))

    assert "A1" in agent._confirmed and "level0" in agent._had_fire
    releases = published(agent, "/doors", action="release")
    assert sorted(t for t, _ in releases) == ["commands/level1/doors", "commands/level2/doors"]
    assert published(agent, "commands/level0/evacuation")

    # a second confirmed room on the same floor does not re-release the other floors
    await agent.process_alert(alert("A2", **FIRE))
    assert len(published(agent, "/doors", action="release")) == 2
    assert len(prompts) == 2


async def test_same_room_same_severity_not_reassessed_within_cooldown(agent):
    prompts = set_llm(agent, False, 0.3)
    await agent.process_alert(alert("A1"))
    await agent.process_alert(alert("A1"))
    assert len(prompts) == 1
    # a severity change is new information
    await agent.process_alert(alert("A1", severity="confirmed", decision="fire"))
    assert len(prompts) == 2


async def test_confirmed_room_is_never_reassessed(agent):
    prompts = set_llm(agent, True, 0.9)
    await agent.process_alert(alert("A1", **FIRE))
    await agent.process_alert(alert("A1", severity="critical", decision="fire", smoke=40, co=90))
    assert len(prompts) == 1


async def test_doubt_puts_room_on_watch_without_touching_doors(agent):
    set_llm(agent, False, 0.2)
    await agent.process_alert(alert("A1"))
    assert "A1" in agent._watch and "A1" not in agent._confirmed
    assert published(agent, "/doors") == []


async def test_watch_stands_down_on_clear_readings(agent):
    set_llm(agent, False, 0.2)
    await agent.process_alert(alert("A1"))
    agent._watch["A1"]["since"] = time.time() - agent_mod.STAND_DOWN_WAIT_S - 1
    agent._latest["A1"] = {"smoke": 0.5, "co": 0.2}

    await agent._watch_tick(time.time())

    assert "A1" not in agent._watch
    (_, sd), *_ = published(agent, "commands/level0/stand-down")[-1:]
    assert sd["room"] == "A1" and sd["floor_clear"] is True and sd["relock"] is False


async def test_watch_keeps_watching_elevated_readings_and_escalates_only_at_fire_level(agent):
    set_llm(agent, False, 0.2)
    await agent.process_alert(alert("A1"))

    # the false-alarm case: smoke lingering, no CO -> keep watching, no doors
    agent._watch["A1"]["since"] = time.time() - 61
    agent._latest["A1"] = {"smoke": 4.0, "co": 0.1}
    await agent._watch_tick(time.time())
    assert "A1" in agent._watch and "A1" not in agent._confirmed
    assert published(agent, "/doors") == []

    # readings reach the multi-sensor rule -> escalate, own floor + other floors
    agent._watch["A1"]["since"] = time.time() - 61
    agent._latest["A1"] = {"smoke": 20.0, "co": 40.0}
    await agent._watch_tick(time.time())
    assert "A1" in agent._confirmed and "A1" not in agent._watch
    releases = sorted(t for t, _ in published(agent, "/doors", action="release"))
    assert releases == ["commands/level0/doors", "commands/level1/doors", "commands/level2/doors"]


async def test_stand_down_reports_floor_state_and_all_clear_relocks_every_floor(agent):
    set_llm(agent, True, 0.9)
    await agent.process_alert(alert("A1", **FIRE))
    await agent.process_alert(alert("A2", **FIRE))
    set_llm(agent, False, 0.2)
    await agent.process_alert(alert("B1", level="level1"))
    assert set(agent._confirmed) == {"A1", "A2"} and set(agent._watch) == {"B1"}

    await agent._stand_down("A1", "level0", "test")
    agent._confirmed.pop("A1")
    assert published(agent, "/stand-down")[-1][1]["floor_clear"] is False

    await agent._stand_down("A2", "level0", "test")
    agent._confirmed.pop("A2")
    sd = published(agent, "/stand-down")[-1][1]
    assert sd["floor_clear"] is True and sd["relock"] is True  # level0 had a fire
    assert published(agent, "/doors", action="lock") == []  # B1 still on watch

    await agent._stand_down("B1", "level1", "test")
    agent._watch.pop("B1")
    sd = published(agent, "/stand-down")[-1][1]
    assert sd["floor_clear"] is True and sd["relock"] is False  # level1 never had a fire
    locks = sorted(t for t, _ in published(agent, "/doors", action="lock"))
    assert locks == ["commands/level0/doors", "commands/level1/doors", "commands/level2/doors"]
    assert not agent._had_fire and not agent._cross_floor_released


async def test_stand_down_refusal_reinstates_confirmed(agent):
    set_llm(agent, False, 0.2)
    await agent.process_alert(alert("A1"))
    await agent.process_alert(
        {"room": "A1", "floor": "level0", "decision": "fire", "severity": "stand_down_refused"}
    )
    assert "A1" in agent._confirmed and "A1" not in agent._watch


async def test_failover_report_is_logged_not_assessed(agent):
    prompts = set_llm(agent, True, 0.9)
    await agent.process_alert(
        {"room": "_controller", "floor": "level0", "decision": "failover", "severity": "warning"}
    )
    assert prompts == [] and not agent._confirmed


async def test_server_takes_over_silent_floor_and_hands_back(agent):
    agent._start_ts = time.time() - agent_mod.HB_TIMEOUT_S - 1
    now = time.time()
    agent._hb[("level0", "active")] = now
    agent._hb[("level1", "standby")] = now  # level2: nobody

    await agent._heartbeat_tick(now)
    assert agent._takeover == {"level0": False, "level1": False, "level2": True}
    assert "building/level2/#" in agent._mq.subscribed

    agent._hb[("level2", "active")] = time.time()
    await agent._heartbeat_tick(time.time())
    assert agent._takeover["level2"] is False
    assert "building/level2/#" in agent._mq.unsubscribed


async def test_coordinate_actuates_doors_directly_for_floor_under_takeover(agent):
    agent._takeover["level2"] = True
    set_llm(agent, True, 0.9)
    await agent.process_alert(alert("A1", **FIRE))

    acted = sorted(args[0] for name, args, _ in agent.bs.calls if name == "set_actuator_state")
    assert acted == ["level2-D1-door", "level2-D2-door"]
    assert published(agent, "commands/level2/doors") == []
    assert published(agent, "commands/level1/doors", action="release")
