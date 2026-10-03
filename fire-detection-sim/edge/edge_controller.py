"""Edge floor controller — Tier 1 of the two-tier fire detection architecture.

One process per floor (per role). Mirrors a real fire alarm panel:
  - subscribes to this floor's sensor feeds via MQTT
  - threshold filter reduces ~300 rooms to a handful of candidates
  - isolation forest (ONNX) confirms fire vs false alarm in <100 ms
  - on confirmed fire: releases THIS floor's fire doors, sounds alarm,
    alerts the server for cross-floor coordination
  - publishes every decision for logging / IF retraining
  - heartbeats; active/standby pair per floor, standby promotes if
    the active misses 30 s
  - accepts server commands (stand-down, door release) but refuses a
    stand-down while its own readings are still critical

Runs anywhere: Raspberry Pi (ARM) or an x86 node. No fds package needed.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import signal
import threading
import time
from collections import defaultdict, deque

import httpx
import numpy as np
import onnxruntime as ort
import paho.mqtt.client as mqtt

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s [%(name)s] %(message)s")
log = logging.getLogger("edge")
logging.getLogger("httpx").setLevel(logging.WARNING)

# --------------------------------------------------------------------------
# Configuration (env vars, overridable by CLI)
# --------------------------------------------------------------------------
MQTT_BROKER = os.getenv("MQTT_BROKER", "localhost")
MQTT_PORT = int(os.getenv("MQTT_PORT", "31174"))
TOPIC_PREFIX = os.getenv("TOPIC_PREFIX", "building")
BUILDSIM_URL = os.getenv("BUILDSIM_URL", "http://localhost:30909")
ONNX_PATH = os.getenv("ONNX_PATH", "detector.onnx")

TICK_S = float(os.getenv("TICK_S", "5"))
HEARTBEAT_S = float(os.getenv("HEARTBEAT_S", "10"))
PEER_TIMEOUT_S = float(os.getenv("PEER_TIMEOUT_S", "30"))
HISTORY = int(os.getenv("HISTORY", "3"))  # samples per sensor kept for rate features

# Threshold filter — reduce rooms before running IF
TH_SMOKE = float(os.getenv("TH_SMOKE", "2"))
TH_CO = float(os.getenv("TH_CO", "5"))
TH_TEMP = float(os.getenv("TH_TEMP", "25"))

# Decision thresholds
FIRE_SMOKE = float(os.getenv("FIRE_SMOKE", "10"))
FIRE_CO = float(os.getenv("FIRE_CO", "20"))
POSSIBLE_SMOKE = float(os.getenv("POSSIBLE_SMOKE", "5"))
REALERT_FACTOR = float(os.getenv("REALERT_FACTOR", "1.5"))  # re-alert if smoke grows by this
ALERT_REPEAT_S = float(os.environ.get("ALERT_REPEAT_S", "60"))  # heartbeat re-alert

DOOR_RELEASED = os.getenv("DOOR_RELEASED", "closed_unlocked")
DOOR_LOCKED = os.getenv("DOOR_LOCKED", "closed_locked")


class EdgeController:
    def __init__(self, floor: str, role: str):
        self.floor = floor
        self.role = role
        self.peer_role = "standby" if role == "active" else "active"
        self.is_acting = role == "active"
        self.node = os.getenv("NODE_NAME", os.uname().nodename)

        # room -> sensor_type -> deque of recent values
        self.readings: dict[str, dict[str, deque]] = defaultdict(
            lambda: defaultdict(lambda: deque(maxlen=HISTORY))
        )
        self.lock = threading.Lock()

        self.door_ids: list[str] = []  # discovered fire-door actuator IDs on this floor
        self.confirmed: dict[str, dict] = {}  # room -> {"smoke":..., "co":..., "ts":...}
        self.doors_released = False
        self._alert_sent = {}  # room -> {severity, smoke, ts}
        self.own_release = False  # doors released for a fire on THIS floor
        self.server_release = False  # doors released by server (fire elsewhere)
        self.last_peer_hb = 0.0
        self.pending_cmds: asyncio.Queue = asyncio.Queue()
        self.loop: asyncio.AbstractEventLoop | None = None

        self.session = ort.InferenceSession(ONNX_PATH)
        self.input_name = self.session.get_inputs()[0].name
        log.info(f"ONNX loaded: {ONNX_PATH}")

        self.http = httpx.AsyncClient(base_url=BUILDSIM_URL, timeout=5.0)

        self.mq = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"edge-{floor}-{role}")
        self.mq.on_connect = self._on_connect
        self.mq.on_message = self._on_message

    # ------------------------------------------------------------------ MQTT
    def _on_connect(self, client, userdata, flags, rc, props=None):
        log.info(f"MQTT connected rc={rc}")
        client.subscribe(f"{TOPIC_PREFIX}/{self.floor}/#", qos=1)
        client.subscribe(f"commands/{self.floor}/#", qos=1)
        client.subscribe(f"heartbeat/{self.floor}/{self.peer_role}", qos=0)

    def _on_message(self, client, userdata, msg):
        try:
            payload = json.loads(msg.payload.decode())
        except Exception:
            return
        topic = msg.topic

        if topic.startswith(f"{TOPIC_PREFIX}/{self.floor}/"):
            room = payload.get("room")
            stype = payload.get("type")
            val = payload.get("value")
            if room is None or stype is None or val is None:
                return
            with self.lock:
                self.readings[room][stype].append(float(val))

        elif topic.startswith(f"heartbeat/{self.floor}/"):
            self.last_peer_hb = time.time()

        elif topic.startswith(f"commands/{self.floor}/"):
            cmd = topic.rsplit("/", 1)[-1]
            if self.loop:
                self.loop.call_soon_threadsafe(self.pending_cmds.put_nowait, (cmd, payload))

    @staticmethod
    def _safe(room: str) -> str:
        """MQTT topics cannot contain + or #; the gateway sanitises the same way."""
        return room.replace("+", "_").replace("#", "_")

    def publish(self, topic: str, obj: dict, qos: int = 1):
        obj.setdefault("floor", self.floor)
        obj.setdefault("role", self.role)
        obj.setdefault("node", self.node)
        obj.setdefault("ts", time.time())
        self.mq.publish(topic, json.dumps(obj), qos=qos)

    # ------------------------------------------------------------- Detection
    def _features(self, hist: dict[str, deque]) -> np.ndarray | None:
        s, c, t = hist.get("smoke"), hist.get("co"), hist.get("temperature")
        if not s or not c or not t:
            return None
        return np.array(
            [[s[-1], c[-1], t[-1], s[-1] - s[0], c[-1] - c[0], t[-1] - t[0]]],
            dtype=np.float32,
        )

    def _if_anomaly(self, feats: np.ndarray) -> bool:
        out = self.session.run(None, {self.input_name: feats})
        return int(np.asarray(out[0]).ravel()[0]) == -1

    def _decide(self, room: str, hist: dict[str, deque]) -> tuple[str, dict]:
        """Returns (decision, snapshot). decision in {fire, possible, normal}."""
        smoke, co, temp = hist["smoke"][-1], hist["co"][-1], hist["temperature"][-1]
        snap = {"smoke": smoke, "co": co, "temperature": temp}

        feats = self._features(hist)
        if feats is None:
            return "normal", snap
        t0 = time.perf_counter()
        anomaly = self._if_anomaly(feats)
        snap["if_ms"] = round((time.perf_counter() - t0) * 1000, 2)
        snap["if_anomaly"] = anomaly

        # Deterministic multi-sensor rule: never vetoed by the learned model.
        if smoke >= FIRE_SMOKE and co >= FIRE_CO:
            return "fire", snap
        # Ambiguous band: the IF decides whether it is worth a Tier-2 (LLM) assessment.
        if anomaly and smoke >= POSSIBLE_SMOKE:
            return "possible", snap
        # Safety net: very high single-sensor smoke goes to the server even if the IF is quiet.
        if smoke >= FIRE_SMOKE:
            return "possible", snap
        return "normal", snap

    def _readings_critical(self, room: str) -> bool:
        with self.lock:
            h = self.readings.get(room)
            if not h or not h.get("smoke") or not h.get("co"):
                return False
            return h["smoke"][-1] >= FIRE_SMOKE and h["co"][-1] >= FIRE_CO

    # ---------------------------------------------------------------- Actions
    async def _discover_doors(self):
        """Learn which fire-door actuators exist on this floor (a panel knows its loop)."""
        try:
            r = await self.http.get("/api/equipment")
            r.raise_for_status()
            ids = []
            for eq in r.json():
                if eq.get("level") != self.floor:
                    continue
                for a in eq.get("actuators", []) or []:
                    if a.get("type") == "fire_door":
                        ids.append(a["id"])
            self.door_ids = ids
            log.info(f"Discovered {len(ids)} fire doors on {self.floor}")
        except Exception as e:
            log.warning(f"Door discovery failed ({e}); will derive from room list")

    async def _set_doors(self, state: str):
        """Release (or re-lock) every fire door on THIS floor."""
        if self.door_ids:
            ids = list(self.door_ids)
        else:
            with self.lock:
                ids = [f"{self.floor}-{r}-door" for r in self.readings.keys()]
        sem = asyncio.Semaphore(16)

        async def one(aid):
            async with sem:
                try:
                    r = await self.http.put(f"/api/actuators/{aid}/state", json={"state": state})
                    return r.status_code < 300
                except Exception:
                    return False

        results = await asyncio.gather(*(one(a) for a in ids))
        ok = sum(1 for x in results if x)
        log.warning(f"DOORS {state}: {ok}/{len(ids)} on {self.floor}")
        self.doors_released = state == DOOR_RELEASED
        # Door state for the operations dashboard (no smoke key, so the server ignores it)
        door_state = "doors_released" if self.doors_released else "doors_locked"
        self.publish(
            f"decisions/{self.floor}/_doors", {"room": "_doors", "decision": door_state}, qos=1
        )

    def _publish_alert(self, room: str, payload: dict, **kw):
        """Alert the server on state change, on a REALERT_FACTOR rise, or every ALERT_REPEAT_S."""
        sev = payload.get("severity", "")
        now = time.time()
        last = self._alert_sent.get(room)
        if last and sev != "stand_down_refused":
            same = last["severity"] == sev
            grown = payload.get("smoke", 0) >= last["smoke"] * REALERT_FACTOR
            if same and not grown and now - last["ts"] < ALERT_REPEAT_S:
                return
        self._alert_sent[room] = {"severity": sev, "smoke": payload.get("smoke", 0), "ts": now}
        self.publish(f"alerts/{self.floor}/{self._safe(room)}", payload, **kw)

    async def _act_on_fire(self, room: str, snap: dict, escalation: str):
        if not self.is_acting:
            return
        if not self.doors_released:
            await self._set_doors(DOOR_RELEASED)
        self.own_release = True
        self._publish_alert(
            room,
            {"room": room, "decision": "fire", "severity": escalation, **snap},
        )
        log.critical(f"FIRE {escalation} in {self.floor}/{room}: {snap}")

    # -------------------------------------------------------------- Commands
    async def _handle_command(self, cmd: str, payload: dict):
        if cmd == "stand-down":
            room = payload.get("room", "")
            if self._readings_critical(room):
                self._publish_alert(
                    room,
                    {
                        "room": room,
                        "decision": "fire",
                        "severity": "stand_down_refused",
                        "reason": "readings still critical",
                    },
                )
                log.warning(f"REFUSED stand-down for {room}: readings still critical")
                return
            self.confirmed.pop(room, None)
            self._alert_sent.pop(room, None)
            if not self.confirmed:
                self.own_release = False
                if (
                    self.is_acting
                    and not self.server_release
                    and payload.get("floor_clear", True)
                    and (self.doors_released or payload.get("relock"))
                ):
                    await self._set_doors(DOOR_LOCKED)
            self.publish(
                f"decisions/{self.floor}/{self._safe(room)}",
                {"room": room, "decision": "stand_down_ack"},
            )
            log.info(f"STAND-DOWN accepted for {room}")

        elif cmd == "doors":
            action = payload.get("action")
            if not self.is_acting:
                return
            if action == "release":
                self.server_release = True
                if not self.doors_released:
                    await self._set_doors(DOOR_RELEASED)
                    log.warning("Cross-floor door release commanded by server")
            elif action == "lock":
                self.server_release = False
                if not self.confirmed:  # authoritative: survives controller restart/failover
                    self.own_release = False
                    await self._set_doors(DOOR_LOCKED)
                    log.warning("Re-lock commanded by server (all clear)")

        elif cmd == "evacuation":
            log.info(f"Evacuation route received: {payload.get('route')}")

    # ----------------------------------------------------------------- Loops
    async def tick_loop(self):
        while True:
            await asyncio.sleep(TICK_S)
            if not self.door_ids:
                await self._discover_doors()  # server may register actuators after we start
            with self.lock:
                snapshot = {
                    r: {k: deque(v) for k, v in h.items()} for r, h in self.readings.items()
                }

            candidates = [
                r
                for r, h in snapshot.items()
                if h.get("smoke")
                and h.get("co")
                and h.get("temperature")
                and (
                    h["smoke"][-1] >= TH_SMOKE
                    or h["co"][-1] >= TH_CO
                    or h["temperature"][-1] >= TH_TEMP
                )
            ]

            # Rooms that just dropped below threshold: tell the server once, explicitly.
            gone = getattr(self, "_prev_candidates", set()) - set(candidates)
            for room in gone:
                h = snapshot.get(room, {})

                def last(k):
                    return round(float(h[k][-1]), 2) if h.get(k) else 0.0

                self.publish(
                    f"decisions/{self.floor}/{self._safe(room)}",
                    {
                        "room": room,
                        "decision": "clear",
                        "smoke": last("smoke"),
                        "co": last("co"),
                        "temperature": last("temperature"),
                    },
                    qos=1,
                )
                self._alert_sent.pop(room, None)
                log.info(f"CLEAR {self.floor}/{room}: below thresholds")
            self._prev_candidates = set(candidates)

            fires = 0
            for room in candidates:
                decision, snap = self._decide(room, snapshot[room])
                self.publish(
                    f"decisions/{self.floor}/{self._safe(room)}",
                    {"room": room, "decision": decision, **snap},
                    qos=0,
                )

                if decision == "fire":
                    fires += 1
                    prev = self.confirmed.get(room)
                    if prev is None:
                        self.confirmed[room] = {**snap, "ts": time.time()}
                        await self._act_on_fire(room, snap, "confirmed")
                    elif snap["smoke"] >= prev["smoke"] * REALERT_FACTOR:
                        self.confirmed[room] = {**snap, "ts": time.time()}
                        await self._act_on_fire(room, snap, "critical")
                elif decision == "possible" and self.is_acting and room not in self.confirmed:
                    self._publish_alert(
                        room,
                        {"room": room, "decision": "possible", "severity": "warning", **snap},
                    )

            log.info(
                f"tick rooms={len(snapshot)} candidates={len(candidates)} "
                f"fires={fires} acting={self.is_acting}"
            )

    async def heartbeat_loop(self):
        while True:
            self.publish(
                f"heartbeat/{self.floor}/{self.role}",
                {
                    "status": "ok",
                    "acting": self.is_acting,
                    "rooms": len(self.readings),
                    "fires": len(self.confirmed),
                },
                qos=0,
            )
            await asyncio.sleep(HEARTBEAT_S)

    async def peer_watch_loop(self):
        """Standby promotes itself if the active goes silent; demotes when it returns."""
        while True:
            await asyncio.sleep(5)
            silent = (time.time() - self.last_peer_hb) > PEER_TIMEOUT_S
            if self.role == "standby":
                if silent and not self.is_acting and self.last_peer_hb > 0:
                    self.is_acting = True
                    log.critical(f"PROMOTED: {self.floor} active missed {PEER_TIMEOUT_S}s")
                    self.publish(
                        f"alerts/{self.floor}/_controller",
                        {"decision": "failover", "severity": "warning"},
                    )
                elif not silent and self.is_acting:
                    self.is_acting = False
                    log.warning(f"DEMOTED: {self.floor} active is back")

    async def command_loop(self):
        while True:
            cmd, payload = await self.pending_cmds.get()
            try:
                await self._handle_command(cmd, payload)
            except Exception as e:
                log.error(f"command {cmd} failed: {e}")

    async def run(self):
        self.loop = asyncio.get_running_loop()
        await self._discover_doors()
        delay = 2
        while True:  # DNS not ready, broker down, network blip: keep trying, never crash
            try:
                self.mq.connect(MQTT_BROKER, MQTT_PORT, keepalive=30)
                break
            except OSError as e:
                log.warning(f"MQTT connect failed: {e}; retry in {delay}s")
                await asyncio.sleep(delay)
                delay = min(delay * 2, 30)
        self.mq.loop_start()
        log.info(
            f"Edge controller {self.floor}/{self.role} on {self.node} "
            f"(acting={self.is_acting}, broker={MQTT_BROKER}:{MQTT_PORT})"
        )
        loops = asyncio.gather(
            self.tick_loop(),
            self.heartbeat_loop(),
            self.peer_watch_loop(),
            self.command_loop(),
        )
        stopping = False

        def _request_stop(sig):
            nonlocal stopping
            stopping = True
            log.info(f"{sig.name} received; shutting down")
            loops.cancel()

        for sig in (signal.SIGTERM, signal.SIGINT):
            self.loop.add_signal_handler(sig, _request_stop, sig)
        try:
            await loops
        except asyncio.CancelledError:
            if not stopping:  # cancelled by our caller, not by a signal
                raise
        finally:
            self.mq.loop_stop()
            self.mq.disconnect()
            await self.http.aclose()
            log.info(f"Edge controller {self.floor}/{self.role} stopped")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--floor", default=os.getenv("FLOOR", "level0"))
    p.add_argument("--role", default=os.getenv("ROLE", "active"), choices=["active", "standby"])
    a = p.parse_args()
    try:
        asyncio.run(EdgeController(a.floor, a.role).run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
