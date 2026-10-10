"""Test doubles and helpers for the Tier-2 agent: no broker, no BuildSim, no DB, no LLM.

The `agent` fixture lives in conftest.py."""

import json
import re

from fds.models.evacuation import EvacuationRoute
from fds.models.spread import RoomRisk


class FakeMQTT:
    """Records publishes as (topic, payload-dict); subscriptions as topic strings."""

    def __init__(self, *args, **kwargs):
        self.published: list[tuple[str, dict]] = []
        self.subscribed: list[str] = []
        self.unsubscribed: list[str] = []

    def publish(self, topic, payload, qos=0):
        self.published.append((topic, json.loads(payload)))

    def subscribe(self, topic, qos=0):
        self.subscribed.append(topic)

    def unsubscribe(self, topic):
        self.unsubscribed.append(topic)

    def connect(self, *args, **kwargs):
        pass

    def loop_start(self):
        pass

    def loop_stop(self):
        pass

    def disconnect(self):
        pass


class FakeBuildSim:
    """Any method call is accepted, recorded as (name, args, kwargs) and returns {}."""

    def __init__(self):
        self.calls: list[tuple[str, tuple, dict]] = []

    def __getattr__(self, name):
        async def call(*args, **kwargs):
            self.calls.append((name, args, kwargs))
            return {}

        return call


class FakeSpread:
    def predict_spread(self, room, level, temperature=None, elapsed=0):
        return [RoomRisk(room="A2", room_id=1, level=level, distance=1.0, risk_score=0.6)]


class FakeEvac:
    def find_routes(self, room, blocked_rooms=None):
        return [
            EvacuationRoute(
                from_room=room, to_exit="EXIT", path_rooms=["A3", "EXIT"], total_weight=5.0
            )
        ]


def alert(
    room, level="level0", severity="warning", decision="possible", smoke=6.0, co=1.0, temp=15.0
):
    """An edge alert payload as edge_controller publishes it."""
    return {
        "room": room,
        "floor": level,
        "decision": decision,
        "severity": severity,
        "smoke": smoke,
        "co": co,
        "temperature": temp,
        "if_anomaly": True,
        "if_ms": 1.0,
        "node": "edge-node-1",
        "role": "active",
    }


def set_llm(agent, is_fire, confidence):
    """Replace the LLM with a fixed verdict; returns the list of prompts it was asked."""
    prompts = []

    async def generate_json(prompt, system=None):
        prompts.append(prompt)
        m = re.search(r"readings for room (\S+) on (\S+):", prompt)
        room, level = (m.group(1), m.group(2)) if m else ("?", "?")
        return {
            "assessment": {"is_fire": is_fire, "confidence": confidence, "reasoning": "test"},
            "affected_room": room,
            "affected_level": level,
            "plan": {"doors": [{"room": room, "action": "unlock"}], "evacuate": is_fire},
            "alert": {"severity": "warning", "title": f"Fire in {room}", "message": "test"},
        }

    agent.llm.generate_json = generate_json
    return prompts


def published(agent, topic_suffix=None, action=None):
    """Published (topic, payload) pairs, optionally filtered by topic suffix / door action."""
    out = []
    for topic, payload in agent._mq.published:
        if topic_suffix and not topic.endswith(topic_suffix):
            continue
        if action and payload.get("action") != action:
            continue
        out.append((topic, payload))
    return out
