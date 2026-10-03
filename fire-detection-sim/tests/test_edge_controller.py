"""Tier-1 edge controller: decision rule, alert gate, door ownership, failover, resilience.

Runs offline: the ONNX session, the MQTT client and the BuildSim HTTP client are faked.
"""

import asyncio
import json
import sys
import time
from collections import deque
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "edge"))
import edge_controller as ec  # noqa: E402


class FakeSession:
    anomaly = False  # set per test: True -> the IF flags the window

    def get_inputs(self):
        return [type("Input", (), {"name": "features"})()]

    def run(self, outputs, feeds):
        return [np.array([[-1 if FakeSession.anomaly else 1]])]


class FakeMQTT:
    def __init__(self, *args, **kwargs):
        self.published: list[tuple[str, dict]] = []
        self.connect_calls = 0
        self.fail_connects = 0

    def publish(self, topic, payload, qos=0):
        self.published.append((topic, json.loads(payload)))

    def connect(self, *args, **kwargs):
        self.connect_calls += 1
        if self.connect_calls <= self.fail_connects:
            raise OSError("Temporary failure in name resolution")

    def subscribe(self, *args, **kwargs):
        pass

    def loop_start(self):
        pass

    def loop_stop(self):
        pass

    def disconnect(self):
        pass


class FakeResponse:
    status_code = 200

    def json(self):
        return []

    def raise_for_status(self):
        pass


class FakeHTTP:
    def __init__(self, *args, **kwargs):
        self.puts: list[tuple[str, dict]] = []

    async def put(self, url, json=None):
        self.puts.append((url, json))
        return FakeResponse()

    async def get(self, url, **kwargs):
        return FakeResponse()

    async def aclose(self):
        pass


def make_controller(monkeypatch, role="active"):
    FakeSession.anomaly = False
    monkeypatch.setattr(ec.ort, "InferenceSession", lambda path: FakeSession())
    monkeypatch.setattr(ec.mqtt, "Client", FakeMQTT)
    monkeypatch.setattr(ec.httpx, "AsyncClient", FakeHTTP)
    c = ec.EdgeController("level0", role)
    c.door_ids = [f"level0-D{i}-door" for i in range(3)]
    return c


@pytest.fixture
def edge(monkeypatch):
    return make_controller(monkeypatch)


@pytest.fixture
def fast_sleep(monkeypatch):
    """Shrink every asyncio.sleep so the controller's loops run in milliseconds."""
    real = asyncio.sleep
    monkeypatch.setattr(asyncio, "sleep", lambda s: real(min(s, 0.002)))
    return real


def hist(smoke, co, temp, first=None):
    f = first or (smoke, co, temp)
    return {
        "smoke": deque([f[0], smoke]),
        "co": deque([f[1], co]),
        "temperature": deque([f[2], temp]),
    }


def door_states(edge):
    return [body["state"] for _, body in edge.http.puts]


# ------------------------------------------------------------- decision rule
def test_multi_sensor_rule_is_never_vetoed_by_the_model(edge):
    FakeSession.anomaly = False
    decision, snap = edge._decide("A1", hist(12, 25, 20))
    assert decision == "fire"
    assert snap["if_anomaly"] is False and snap["if_ms"] >= 0


def test_model_gates_the_ambiguous_band(edge):
    FakeSession.anomaly = True
    assert edge._decide("A1", hist(6, 1, 15))[0] == "possible"
    FakeSession.anomaly = False
    assert edge._decide("A1", hist(6, 1, 15))[0] == "normal"


def test_high_smoke_alone_still_reaches_the_server(edge):
    FakeSession.anomaly = False
    assert edge._decide("A1", hist(15, 0.5, 15))[0] == "possible"


# ---------------------------------------------------------------- alert gate
def test_alert_gate_suppresses_repeats_and_passes_changes(edge):
    pub = edge.mq.published
    edge._publish_alert("A1", {"room": "A1", "severity": "warning", "smoke": 6.0})
    edge._publish_alert("A1", {"room": "A1", "severity": "warning", "smoke": 6.5})
    assert len(pub) == 1
    edge._publish_alert("A1", {"room": "A1", "severity": "warning", "smoke": 10.0})  # >= 1.5x
    assert len(pub) == 2
    edge._publish_alert("A1", {"room": "A1", "severity": "confirmed", "smoke": 10.0})
    assert len(pub) == 3
    edge._publish_alert("A1", {"room": "A1", "severity": "stand_down_refused"})
    assert len(pub) == 4
    assert {t for t, _ in pub} == {"alerts/level0/A1"}


def test_alert_gate_repeats_after_alert_repeat_s(edge):
    edge._publish_alert("A1", {"room": "A1", "severity": "warning", "smoke": 6.0})
    edge._alert_sent["A1"]["ts"] -= ec.ALERT_REPEAT_S + 1
    edge._publish_alert("A1", {"room": "A1", "severity": "warning", "smoke": 6.0})
    assert len(edge.mq.published) == 2


def test_topics_never_contain_mqtt_wildcards(edge):
    edge._publish_alert("A103+A112", {"room": "A103+A112", "severity": "warning", "smoke": 6.0})
    topic, payload = edge.mq.published[-1]
    assert topic == "alerts/level0/A103_A112" and payload["room"] == "A103+A112"


# ------------------------------------------------------------ door ownership
async def test_own_fire_releases_doors_once(edge):
    await edge._act_on_fire("A1", {"smoke": 12, "co": 25}, "confirmed")
    await edge._act_on_fire("A2", {"smoke": 12, "co": 25}, "confirmed")
    assert door_states(edge) == [ec.DOOR_RELEASED] * 3
    assert edge.doors_released and edge.own_release and not edge.server_release
    door_events = [p for t, p in edge.mq.published if t == "decisions/level0/_doors"]
    assert door_events[-1]["decision"] == "doors_released"


async def test_stand_down_relocks_only_when_floor_clear(edge):
    edge.confirmed = {"A1": {}, "A2": {}}
    edge.doors_released = edge.own_release = True
    await edge._handle_command("stand-down", {"room": "A1", "floor_clear": False})
    assert edge.http.puts == [] and "A1" not in edge.confirmed
    await edge._handle_command("stand-down", {"room": "A2", "floor_clear": True})
    assert door_states(edge) == [ec.DOOR_LOCKED] * 3
    assert edge.mq.published[-1][1]["decision"] == "stand_down_ack"


async def test_stand_down_keeps_doors_open_while_server_release_active(edge):
    edge.confirmed = {"A1": {}}
    edge.doors_released = edge.own_release = edge.server_release = True
    await edge._handle_command("stand-down", {"room": "A1", "floor_clear": True})
    assert edge.http.puts == []


async def test_restarted_controller_relocks_on_server_hint(edge):
    # fresh process after failover: no memory of having released anything
    await edge._handle_command("stand-down", {"room": "A1", "floor_clear": True, "relock": True})
    assert door_states(edge) == [ec.DOOR_LOCKED] * 3


async def test_server_release_is_idempotent_and_lock_is_authoritative(edge):
    await edge._handle_command("doors", {"action": "release"})
    await edge._handle_command("doors", {"action": "release"})
    assert door_states(edge) == [ec.DOOR_RELEASED] * 3 and edge.server_release
    edge.http.puts.clear()
    await edge._handle_command("doors", {"action": "lock"})
    assert door_states(edge) == [ec.DOOR_LOCKED] * 3
    assert not edge.server_release and not edge.doors_released


async def test_lock_command_ignored_while_own_fire_confirmed(edge):
    edge.confirmed = {"A1": {}}
    await edge._handle_command("doors", {"action": "release"})
    edge.http.puts.clear()
    await edge._handle_command("doors", {"action": "lock"})
    assert edge.http.puts == []


async def test_standby_never_touches_doors(monkeypatch):
    standby = make_controller(monkeypatch, role="standby")
    await standby._handle_command("doors", {"action": "release"})
    await standby._act_on_fire("A1", {"smoke": 12, "co": 25}, "confirmed")
    assert standby.http.puts == [] and not standby.doors_released


async def test_stand_down_refused_while_readings_critical(edge):
    edge.readings["A1"]["smoke"].append(15.0)
    edge.readings["A1"]["co"].append(30.0)
    edge.confirmed = {"A1": {}}
    await edge._handle_command("stand-down", {"room": "A1", "floor_clear": True})
    assert "A1" in edge.confirmed and edge.http.puts == []
    assert edge.mq.published[-1][1]["severity"] == "stand_down_refused"


# ------------------------------------------------------ messaging / failover
def test_on_message_routes_readings_and_heartbeats(edge):
    class Msg:
        def __init__(self, topic, payload):
            self.topic = topic
            self.payload = payload if isinstance(payload, bytes) else json.dumps(payload).encode()

    edge._on_message(
        None, None, Msg("building/level0/A1/smoke", {"room": "A1", "type": "smoke", "value": 3.2})
    )
    assert list(edge.readings["A1"]["smoke"]) == [3.2]
    edge._on_message(None, None, Msg("heartbeat/level0/standby", {"status": "ok"}))
    assert time.time() - edge.last_peer_hb < 1.0
    edge._on_message(None, None, Msg("building/level0/A1/smoke", b"not json"))  # ignored
    assert list(edge.readings["A1"]["smoke"]) == [3.2]


async def test_standby_promotes_when_active_is_silent_and_demotes_on_return(
    monkeypatch, fast_sleep
):
    standby = make_controller(monkeypatch, role="standby")
    monkeypatch.setattr(ec, "PEER_TIMEOUT_S", 0.05)
    standby.last_peer_hb = time.time() - 1.0
    task = asyncio.create_task(standby.peer_watch_loop())
    try:
        await fast_sleep(0.05)
        assert standby.is_acting
        assert any(p.get("decision") == "failover" for _, p in standby.mq.published)
        standby.last_peer_hb = time.time()
        await fast_sleep(0.05)
        assert not standby.is_acting
    finally:
        task.cancel()


async def test_run_retries_broker_connection_instead_of_crashing(edge, fast_sleep):
    edge.mq.fail_connects = 2
    task = asyncio.create_task(edge.run())
    for _ in range(500):
        await fast_sleep(0.005)
        if edge.mq.connect_calls >= 3:
            break
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert edge.mq.connect_calls == 3


async def test_tick_publishes_clear_when_room_drops_below_threshold(edge, fast_sleep):
    FakeSession.anomaly = False
    for k, v in (("smoke", 3.0), ("co", 0.5), ("temperature", 15.0)):
        edge.readings["A1"][k].extend([v, v])
    task = asyncio.create_task(edge.tick_loop())
    try:
        await fast_sleep(0.05)
        decisions = [p for t, p in edge.mq.published if t == "decisions/level0/A1"]
        assert decisions and decisions[-1]["decision"] == "normal"
        edge.readings["A1"]["smoke"].extend([0.1, 0.1])  # last value below TH_SMOKE
        await fast_sleep(0.05)
        decisions = [p for t, p in edge.mq.published if t == "decisions/level0/A1"]
        assert decisions[-1]["decision"] == "clear"
    finally:
        task.cancel()


def test_snapshot_flags_sensors_frozen_for_stale_s(edge):
    for v in (5.0, 5.09, 5.09):
        for k in ("smoke", "co", "temperature"):
            edge.readings["A1"][k].append(v)
    edge._last_change[("A1", "smoke")] = time.time()
    edge._last_change[("A1", "co")] = time.time() - ec.STALE_S - 1
    _, snap = edge._decide("A1", edge.readings["A1"])
    assert snap["stale"] == ["co"]
