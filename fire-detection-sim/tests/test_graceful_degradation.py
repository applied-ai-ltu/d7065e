"""Graceful degradation: the response never depends on the LLM being reachable.

When LiteLLM is down the server falls back to a rule-based assessment. The edge has
already acted on its own floor by then; this covers what the server still does.
"""

from tests.agent_fakes import alert, published


def break_llm(agent):
    async def unreachable(prompt, system=None):
        raise ConnectionError("LiteLLM unreachable")

    agent.llm.generate_json = unreachable


async def test_fallback_confirms_unambiguous_fire(agent):
    break_llm(agent)
    await agent.process_alert(alert("A1", severity="confirmed", decision="fire", smoke=40, co=100))
    assert "A1" in agent._confirmed
    assert agent._last_plan["assessment"]["reasoning"].startswith("Fallback")
    releases = sorted(t for t, _ in published(agent, "/doors", action="release"))
    assert releases == ["commands/level1/doors", "commands/level2/doors"]


async def test_fallback_watches_ambiguous_readings(agent):
    break_llm(agent)
    await agent.process_alert(alert("A1", smoke=6, co=1))
    assert "A1" in agent._watch and "A1" not in agent._confirmed
    assert published(agent, "/doors") == []


async def test_fallback_trusts_the_multi_sensor_rule_at_the_threshold(agent):
    # smoke 12 / CO 25 is the edge's confirmed condition; with the LLM down the server
    # must not second-guess it (scenario 2, 2026-10-03: origin room was doubted while
    # its neighbours were confirmed, so cross-floor release and viz went to the wrong room)
    break_llm(agent)
    await agent.process_alert(alert("A1", severity="confirmed", decision="fire", smoke=12, co=25))
    assert "A1" in agent._confirmed and "A1" not in agent._watch
    assert agent._last_plan["assessment"]["confidence"] >= 0.7
    assert published(agent, "/doors", action="release")


async def test_fallback_rejects_smoke_without_co(agent):
    break_llm(agent)
    await agent.process_alert(alert("A1", severity="confirmed", decision="fire", smoke=40, co=3))
    assert "A1" in agent._watch and "A1" not in agent._confirmed
    assert agent._last_plan["assessment"]["is_fire"] is False
    assert published(agent, "/doors") == []


async def test_fallback_confirms_strong_smoke_with_a_faulty_co_sensor(agent):
    break_llm(agent)
    a = alert("A1", severity="confirmed", decision="fire", smoke=25, co=0.0)
    a["stale"] = ["co"]
    await agent.process_alert(a)
    assert "A1" in agent._confirmed
    assert published(agent, "/doors", action="release")
