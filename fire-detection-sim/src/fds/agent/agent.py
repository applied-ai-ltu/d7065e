"""Server-tier fire agent — Tier 2 of the two-tier architecture.

Event-driven: reacts to alerts published by edge floor controllers over MQTT.
It does NOT poll sensors and does NOT run the isolation forest; the edge does
that in <100 ms and has already released its own floor's doors by the time an
alert arrives here. This tier enhances and coordinates; it never gates the edge.

Responsibilities
  - LLM assessment of each edge alert (real fire vs false alarm, plan)
  - Cross-floor coordination: door release on OTHER floors, evacuation routes
  - Safety-asymmetric override: escalate immediately, de-escalate only after
    STAND_DOWN_WAIT_S of clear readings (the edge may still refuse)
  - Heartbeat supervision of the floor controllers; takes over a floor when
    both its controllers are silent, hands back when one returns
  - BuildSim visualisation (highlights, effects, coverage, alerts)
  - Persists fire_events and llm_overrides to TimescaleDB
"""

import asyncio
import json
import logging
import os
import threading
import time
from collections import defaultdict, deque
from datetime import UTC, datetime

import asyncpg
import paho.mqtt.client as mqtt

from fds.agent.llm import LLMClient
from fds.agent.prompts import FIRE_AGENT_SYSTEM
from fds.buildsim.client import BuildSimClient
from fds.models.evacuation import EvacuationPlanner
from fds.models.spread import FireSpreadPredictor

logger = logging.getLogger(__name__)

STAND_DOWN_WAIT_S = 60.0  # de-escalation needs this long of clear readings
ALERT_COOLDOWN_S = 60  # same room + same severity -> one LLM assessment per minute
LLM_CONCURRENCY = int(os.environ.get("LLM_CONCURRENCY", "3"))  # parallel Tier-2 assessments
CLEAR_SMOKE = 2.0
CLEAR_CO = 5.0
HB_TIMEOUT_S = 30.0  # a controller is "silent" after this
# After its own start the server waits this long before it declares a floor silent, so a
# cold start (edges pulling images on the Pi) does not begin with a takeover of every floor.
BOOT_GRACE_S = float(os.getenv("BOOT_GRACE_S", "90"))
TAKEOVER_FIRE_SMOKE = 10.0  # server-side threshold rules while covering a floor
TAKEOVER_FIRE_CO = 20.0
DOOR_RELEASED = "closed_unlocked"
DOOR_LOCKED = "closed_locked"


class FireAgent:
    def __init__(
        self,
        bs: BuildSimClient,
        spread_predictor: FireSpreadPredictor,
        evacuation_planner: EvacuationPlanner,
        detector=None,  # kept for call-compat; the edge runs the IF now
        llm_url="https://localhost:30400",
        llm_api_key="",
        model="qwen2.5-14b",
        tick_interval=5.0,
        db_config=None,
        mqtt_host="localhost",
        mqtt_port=31174,
        topic_prefix="building",
        levels=("level0", "level1", "level2"),
    ):
        self.bs = bs
        self.spread = spread_predictor
        self.evac = evacuation_planner
        self.llm = LLMClient(base_url=llm_url, api_key=llm_api_key, model=model)
        self.tick_interval = tick_interval
        self.db_config = db_config
        self.mqtt_host = mqtt_host
        self.mqtt_port = mqtt_port
        self.topic_prefix = topic_prefix
        self.levels = list(levels)

        self._running = False
        self._episode_log = []
        self._active_fire = False
        self._fire_start_time = {}
        self._last_alert = {}  # room -> (severity, ts)
        self._cross_floor_released = set()
        self._had_fire = set()  # levels with a confirmed room this incident
        self._last_llm_ms = None  # inference time of the most recent LLM call
        # BuildSim visualisation state per confirmed room; pushed as whole lists (replace semantics)
        self._viz = {"appearance": {}, "effects": {}, "alerts": {}, "coverage": {}}
        self._last_plan = None

        # Room -> level, from run(rooms). Fixes cross-floor visualisation payloads.
        self.room_levels: dict[str, str] = {}
        # Latest readings per room, from the edge's decisions/ stream (server-side view)
        self._latest: dict[str, dict] = {}
        self._hist: dict[str, deque] = defaultdict(lambda: deque(maxlen=3))
        # Rooms the LLM doubted: room -> {"level", "since"} — delayed stand-down
        self._watch: dict[str, dict] = {}
        # Rooms confirmed as fire: room -> {"level", "clear_since"}
        self._confirmed: dict[str, dict] = {}
        # Heartbeats: (level, role) -> last ts
        self._hb: dict[tuple, float] = {}
        self._takeover: dict[str, bool] = {lv: False for lv in self.levels}
        self._takeover_readings: dict[str, dict[str, deque]] = defaultdict(
            lambda: defaultdict(lambda: deque(maxlen=3))
        )
        self._door_ids: dict[str, list[str]] = defaultdict(list)
        self._start_ts = 0.0
        self._lock = threading.Lock()
        self._queue: asyncio.Queue = asyncio.Queue()
        self._loop: asyncio.AbstractEventLoop | None = None

        self._mq = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="fire-agent-server")
        self._mq.on_connect = self._on_connect
        self._mq.on_message = self._on_message

    # ------------------------------------------------------------------ MQTT
    def _on_connect(self, client, userdata, flags, rc, props=None):
        logger.info(f"Agent MQTT connected rc={rc}")
        client.subscribe("alerts/#", qos=1)
        client.subscribe("decisions/#", qos=0)
        client.subscribe("heartbeat/#", qos=0)

    def _on_message(self, client, userdata, msg):
        try:
            p = json.loads(msg.payload.decode())
        except Exception:
            return
        t = msg.topic
        if t.startswith("alerts/"):
            if self._loop:
                self._loop.call_soon_threadsafe(self._queue.put_nowait, p)
        elif t.startswith("decisions/"):
            room = p.get("room")
            if room and "smoke" in p:
                with self._lock:
                    self._latest[room] = p
                    self._hist[room].append(p)
        elif t.startswith("heartbeat/"):
            parts = t.split("/")
            if len(parts) == 3:
                self._hb[(parts[1], parts[2])] = time.time()
        elif t.startswith(f"{self.topic_prefix}/"):
            # Only subscribed while covering a floor
            room, stype, val, level = p.get("room"), p.get("type"), p.get("value"), p.get("level")
            if room and stype and val is not None and self._takeover.get(level):
                with self._lock:
                    self._takeover_readings[room][stype].append(float(val))

    def _publish(self, topic: str, obj: dict, qos: int = 1):
        obj.setdefault("ts", time.time())
        obj.setdefault("source", "server")
        self._mq.publish(topic, json.dumps(obj), qos=qos)

    @staticmethod
    def _safe(room: str) -> str:
        return room.replace("+", "_").replace("#", "_")

    # --------------------------------------------------------------- startup
    async def _discover_doors(self):
        try:
            eqs = await self.bs._get("/api/equipment")
            for eq in eqs:
                lv = eq.get("level")
                for a in eq.get("actuators", []) or []:
                    if a.get("type") == "fire_door" and lv:
                        self._door_ids[lv].append(a["id"])
            logger.info(f"Doors discovered: { {k: len(v) for k, v in self._door_ids.items()} }")
        except Exception as e:
            logger.warning(f"Door discovery failed: {e}")

    async def run(self, rooms):
        """rooms: list of (room, level). Kept for call-compat with run_full."""
        self._running = True
        self._loop = asyncio.get_running_loop()
        self._start_ts = time.time()
        for room, level in rooms:
            self.room_levels[room] = level

        await self._discover_doors()
        self._mq.connect(self.mqtt_host, self.mqtt_port, keepalive=30)
        self._mq.loop_start()
        logger.info(
            f"Agent started (alert-driven): {len(self.room_levels)} rooms, "
            f"levels={self.levels}, broker={self.mqtt_host}:{self.mqtt_port}"
        )
        try:
            await asyncio.gather(
                self._alert_loop(),
                self._watch_loop(),
                self._heartbeat_loop(),
                self._takeover_loop(),
            )
        finally:
            self._mq.loop_stop()
            self._mq.disconnect()

    def stop(self):
        self._running = False

    def get_episode_log(self):
        return self._episode_log

    # ------------------------------------------------------------ alert path
    async def _alert_loop(self):
        """Assess alerts with bounded concurrency; SGLang batches the parallel calls."""
        sem = asyncio.Semaphore(LLM_CONCURRENCY)
        tasks: set[asyncio.Task] = set()

        async def handle(p):
            async with sem:
                try:
                    await self.process_alert(p)
                except Exception as e:
                    logger.error(f"Alert processing failed: {e}")

        while self._running:
            try:
                p = await asyncio.wait_for(self._queue.get(), timeout=1.0)
            except TimeoutError:
                continue
            t = asyncio.create_task(handle(p))
            tasks.add(t)
            t.add_done_callback(tasks.discard)

    def _features_for(self, room: str, p: dict) -> dict:
        with self._lock:
            hist = list(self._hist.get(room, []))
        first = hist[0] if hist else p
        return {
            "smoke": float(p.get("smoke", 0)),
            "co": float(p.get("co", 0)),
            "temperature": float(p.get("temperature", 0)),
            "smoke_rate": float(p.get("smoke", 0)) - float(first.get("smoke", 0)),
            "co_rate": float(p.get("co", 0)) - float(first.get("co", 0)),
            "temp_rate": float(p.get("temperature", 0)) - float(first.get("temperature", 0)),
            "stale": list(p.get("stale") or []),
        }

    async def process_alert(self, p: dict):
        room = p.get("room", "")
        level = p.get("level") or p.get("floor") or self.room_levels.get(room, "")
        decision = p.get("decision", "")
        severity = p.get("severity", "")
        src = f"{p.get('node', '?')}/{p.get('role', '?')}"

        if room == "_controller" or decision == "failover":
            await self._log_override("_controller", level, "failover", f"from {src}")
            logger.warning(f"FAILOVER on {level} reported by {src}")
            return

        if severity == "stand_down_refused":
            logger.warning(f"Edge REFUSED stand-down for {level}/{room}: readings still critical")
            await self._log_override(room, level, "stand_down_refused", p.get("reason", ""))
            self._watch.pop(room, None)
            self._confirmed[room] = {"level": level, "clear_since": None}
            self._had_fire.add(level)
            return

        now = time.time()
        if room in self._confirmed:
            logger.debug(f"{level}/{room} {severity}: already confirmed, no re-assessment")
            return
        last = self._last_alert.get(room)
        if last and last[0] == severity and now - last[1] < ALERT_COOLDOWN_S:
            return
        self._last_alert[room] = (severity, now)
        logger.info(f"ALERT from {src}: {level}/{room} decision={decision} severity={severity}")
        features = self._features_for(room, p)

        if room not in self._fire_start_time:
            self._fire_start_time[room] = time.time()
        elapsed = time.time() - self._fire_start_time[room]

        # Spread + evacuation (same models as before; fix levels for other floors)
        risk_rooms = self.spread.predict_spread(
            room, level, temperature=features["temperature"], elapsed=elapsed
        )
        for r in risk_rooms:
            r.level = self.room_levels.get(r.room, level)
        logger.info(f"Spread prediction: {[(r.room, r.risk_score) for r in risk_rooms[:5]]}")

        blocked = [r.room for r in risk_rooms if r.risk_score > 0.5]
        evac_routes = self.evac.find_routes(room, blocked_rooms=blocked)
        valid = [r for r in evac_routes if r.total_weight < float("inf")]
        if not valid:
            evac_routes = self.evac.find_routes(room, blocked_rooms=None)
            valid = [r for r in evac_routes if r.total_weight < float("inf")]
        evac_routes = valid or evac_routes
        if valid:
            logger.info(
                f"Evacuation: best route to {valid[0].to_exit} "
                f"via {valid[0].path_rooms[:5]} ({valid[0].total_weight})"
            )

        # LLM assessment
        plan = await self._plan(room, level, features, risk_rooms, evac_routes, edge=p)
        verified, issues = await self._verify(plan)
        if not verified:
            logger.warning(f"Verification failed: {issues}")

        assessment = plan.get("assessment", {})
        is_fire = bool(assessment.get("is_fire"))
        conf = float(assessment.get("confidence", 0) or 0)

        if is_fire and conf >= 0.7:
            # CONFIRM: enhance + coordinate. Edge already acted on its floor.
            self._watch.pop(room, None)
            self._confirmed[room] = {"level": level, "clear_since": None}
            self._had_fire.add(level)
            await self._execute(plan, risk_rooms, evac_routes)
            await self._coordinate(room, level, evac_routes, reason=assessment.get("reasoning", ""))
            await self._log_override(
                room, level, "confirm", f"conf={conf:.2f}", llm_ms=self._last_llm_ms
            )
            if severity == "server_takeover":
                await self._set_floor_doors(level, DOOR_RELEASED)
        else:
            # DOUBT: never undo the edge on one LLM call. Watch, then decide on readings.
            if room not in self._confirmed:
                self._watch[room] = {"level": level, "since": time.time()}
                await self._log_override(
                    room,
                    level,
                    "watch_pending",
                    f"LLM is_fire={is_fire} conf={conf:.2f}; waiting {STAND_DOWN_WAIT_S:.0f}s",
                    llm_ms=self._last_llm_ms,
                )
                logger.warning(
                    f"LLM doubts {level}/{room} (is_fire={is_fire}, conf={conf:.2f}); "
                    f"watching {STAND_DOWN_WAIT_S:.0f}s before any stand-down"
                )
            sev = "warning"
            await self._log_fire_event(room, level, sev, plan)

        self._log_episode(room, features, plan, risk_rooms, evac_routes, verified, issues)
        self._last_plan = plan

    # -------------------------------------------------------- coordination
    async def _release_floor(self, level, room, reason):
        """Release the doors on ONE floor (via its edge, or directly if we cover it)."""
        if self._takeover.get(level):
            await self._set_floor_doors(level, DOOR_RELEASED)
        else:
            self._publish(
                f"commands/{level}/doors", {"action": "release", "reason": reason, "origin": room}
            )

    async def _coordinate(self, room, level, evac_routes, reason=""):
        """Cross-floor response: release doors on the OTHER floors, push routes."""
        newly = []
        for other in self.levels:
            if other == level or other in self._cross_floor_released:
                continue
            if self._takeover.get(other):
                await self._set_floor_doors(other, DOOR_RELEASED)
            else:
                self._publish(
                    f"commands/{other}/doors",
                    {"action": "release", "reason": f"fire on {level}/{room}", "origin": room},
                )
            self._cross_floor_released.add(other)
            newly.append(other)
        if evac_routes:
            best = evac_routes[0]
            self._publish(
                f"commands/{level}/evacuation",
                {"room": room, "route": best.path_rooms, "to_exit": best.to_exit},
            )
        if newly:
            logger.warning(f"CROSS-FLOOR: doors released on {newly} for fire on {level}/{room}")

    async def _watch_loop(self):
        """Safety asymmetry: de-escalate slowly, on readings, never on one LLM opinion."""
        while self._running:
            await asyncio.sleep(5)
            await self._watch_tick(time.time())

    @staticmethod
    def _readings_clear(snap: dict) -> bool:
        """Below the stand-down thresholds, judged on live sensors only.

        The edge lists sensors whose value has been frozen for STALE_S in snap["stale"];
        a stuck CO sensor at 5.09 must not hold three floors of doors open. A room whose
        smoke AND CO are both stale is never declared clear from its readings alone."""
        stale = set(snap.get("stale") or [])
        if {"smoke", "co"} <= stale:
            return False
        ok_smoke = "smoke" in stale or snap.get("smoke", 99) < CLEAR_SMOKE
        ok_co = "co" in stale or snap.get("co", 99) < CLEAR_CO
        return ok_smoke and ok_co

    async def _watch_tick(self, now):
        """One pass of _watch_loop; separated so tests can drive time."""
        # Doubted rooms
        for room, w in list(self._watch.items()):
            if now - w["since"] < STAND_DOWN_WAIT_S:
                continue
            snap = self._latest.get(room, {})
            smoke, co = snap.get("smoke", 99), snap.get("co", 99)
            clear = self._readings_clear(snap)
            fire_level = smoke >= TAKEOVER_FIRE_SMOKE and co >= TAKEOVER_FIRE_CO
            if clear:
                await self._stand_down(room, w["level"], "LLM false alarm + readings clear 60s")
                self._watch.pop(room, None)
            elif fire_level:
                logger.warning(
                    f"ESCALATE {w['level']}/{room}: LLM doubted it but readings at fire level "
                    f"(smoke={smoke}, co={co})"
                )
                self._confirmed[room] = {"level": w["level"], "clear_since": None}
                self._had_fire.add(w["level"])
                await self._release_floor(w["level"], room, "server escalation")
                await self._coordinate(room, w["level"], [], reason="escalated after watch")
                await self._log_override(room, w["level"], "escalated", "escalated: fire-level")
                self._watch.pop(room, None)
            else:
                w["since"] = now  # neither clear nor fire-level: keep watching
                logger.info(
                    f"WATCH {w['level']}/{room}: elevated but not fire-level "
                    f"(smoke={smoke}, co={co}); watching another {STAND_DOWN_WAIT_S}s"
                )
        # Confirmed fires that have gone out
        for room, c in list(self._confirmed.items()):
            snap = self._latest.get(room, {})
            clear = self._readings_clear(snap)
            if clear:
                c["clear_since"] = c["clear_since"] or now
                if now - c["clear_since"] >= STAND_DOWN_WAIT_S:
                    await self._stand_down(room, c["level"], "fire out: readings clear 60s")
                    self._confirmed.pop(room, None)
                    self._fire_start_time.pop(room, None)
            else:
                c["clear_since"] = None

    async def _stand_down(self, room, level, reason):
        others = (set(self._confirmed) | set(self._watch)) - {room}
        if room in self._confirmed:
            self._had_fire.add(level)
        same_floor = [
            r
            for r in others
            if (self._confirmed.get(r) or self._watch.get(r) or {}).get("level") == level
        ]
        floor_clear = not same_floor
        relock = floor_clear and level in self._had_fire
        self._publish(
            f"commands/{level}/stand-down",
            {"room": room, "reason": reason, "floor_clear": floor_clear, "relock": relock},
        )
        await self._clear_room_viz(room)
        await self._log_override(room, level, "stand_down", reason)
        logger.warning(f"STAND-DOWN sent for {level}/{room}: {reason} (floor_clear={floor_clear})")
        if self._takeover.get(level) and floor_clear:
            await self._set_floor_doors(level, DOOR_LOCKED)
        self._last_alert.pop(room, None)
        if not others and (self._cross_floor_released or self._had_fire):
            for lv in self.levels:
                if self._takeover.get(lv):
                    await self._set_floor_doors(lv, DOOR_LOCKED)
                else:
                    self._publish(f"commands/{lv}/doors", {"action": "lock", "reason": "all clear"})
            logger.warning(f"ALL CLEAR: re-lock commanded on {self.levels}")
            self._cross_floor_released.clear()
            self._had_fire.clear()
            self._active_fire = False
            await self._clear_all_viz()

    # ------------------------------------------------------ heartbeat/takeover
    async def _heartbeat_loop(self):
        while self._running:
            await asyncio.sleep(5)
            await self._heartbeat_tick(time.time())

    async def _heartbeat_tick(self, now):
        """One pass of _heartbeat_loop; separated so tests can drive time."""
        for lv in self.levels:
            a = now - self._hb.get((lv, "active"), 0) < HB_TIMEOUT_S
            s = now - self._hb.get((lv, "standby"), 0) < HB_TIMEOUT_S
            seen_any = (lv, "active") in self._hb or (lv, "standby") in self._hb
            grace_over = now - self._start_ts > BOOT_GRACE_S
            if not a and not s and not self._takeover[lv] and (seen_any or grace_over):
                self._takeover[lv] = True
                self._mq.subscribe(f"{self.topic_prefix}/{lv}/#", qos=0)
                await self._log_override(
                    "_controller", lv, "server_takeover", "both floor controllers silent"
                )
                logger.critical(
                    f"SERVER TAKEOVER of {lv}: no edge heartbeat for {HB_TIMEOUT_S:.0f}s"
                )
            elif (a or s) and self._takeover[lv]:
                self._takeover[lv] = False
                self._mq.unsubscribe(f"{self.topic_prefix}/{lv}/#")
                with self._lock:
                    for room in list(self._takeover_readings):
                        if self.room_levels.get(room) == lv:
                            self._takeover_readings.pop(room, None)
                await self._log_override(
                    "_controller", lv, "server_handback", "floor controller heartbeat resumed"
                )
                logger.warning(f"SERVER HANDBACK of {lv}: edge controller is back")

    async def _takeover_loop(self):
        """While covering a floor: threshold rules on raw sensor feed, then normal alert path."""
        while self._running:
            await asyncio.sleep(self.tick_interval)
            active_levels = [lv for lv, on in self._takeover.items() if on]
            if not active_levels:
                continue
            with self._lock:
                snap = {
                    r: {k: list(v) for k, v in h.items()}
                    for r, h in self._takeover_readings.items()
                }
            for room, h in snap.items():
                lv = self.room_levels.get(room)
                if lv not in active_levels or room in self._confirmed or room in self._watch:
                    continue
                if not (h.get("smoke") and h.get("co") and h.get("temperature")):
                    continue
                smoke, co, temp = h["smoke"][-1], h["co"][-1], h["temperature"][-1]
                if smoke >= TAKEOVER_FIRE_SMOKE and co >= TAKEOVER_FIRE_CO:
                    p = {
                        "room": room,
                        "level": lv,
                        "decision": "fire",
                        "severity": "server_takeover",
                        "smoke": smoke,
                        "co": co,
                        "temperature": temp,
                        "node": "server",
                        "role": "takeover",
                    }
                    with self._lock:
                        self._latest[room] = p
                        self._hist[room].append(p)
                    logger.critical(f"TAKEOVER DETECTION {lv}/{room}: smoke={smoke} co={co}")
                    await self._queue.put(p)

    async def _set_floor_doors(self, level, state):
        ids = self._door_ids.get(level, [])
        ok = 0
        for aid in ids:
            try:
                await self.bs.set_actuator_state(aid, state)
                ok += 1
            except Exception:
                pass
        logger.warning(f"SERVER DOORS {state}: {ok}/{len(ids)} on {level}")

    # ------------------------------------------------------------- planning
    async def _plan(self, room, level, features, risk_rooms, evac_routes, edge=None):
        risk_str = (
            ", ".join([f"{r.room} (risk={r.risk_score})" for r in risk_rooms[:5]]) or "none nearby"
        )
        route_str = ""
        if evac_routes:
            best = evac_routes[0]
            route_str = f"Best route: {' -> '.join(best.path_rooms)} (distance={best.total_weight})"
        edge_str = ""
        if edge:
            edge_str = (
                f"\nEdge controller ({edge.get('node', '?')}/{edge.get('role', '?')}) decision: "
                f"{edge.get('decision', '?')} (severity={edge.get('severity', '?')}, "
                f"isolation-forest anomaly={edge.get('if_anomaly', '?')}). "
                f"It has ALREADY released this floor's fire doors if it confirmed a fire."
            )

        stale_str = ", ".join(features.get("stale") or []) or "none"
        prompt = f"""Current sensor readings for room {room} on {level}:
- Smoke: {features.get("smoke", 0):.1f} %obs/m
- CO: {features.get("co", 0):.1f} ppm
- Temperature: {features.get("temperature", 0):.1f} C
- Smoke rate of change: {features.get("smoke_rate", 0):.2f}
- CO rate of change: {features.get("co_rate", 0):.2f}
- Temperature rate of change: {features.get("temp_rate", 0):.2f}
- Faulty sensors (value frozen >= 60 s, do not trust): {stale_str}
{edge_str}
Rooms at risk from fire spread: {risk_str}
Evacuation: {route_str}

Assess whether this is a real fire or a false alarm and provide a response plan."""

        t0 = time.perf_counter()
        try:
            plan = await self.llm.generate_json(prompt, system=FIRE_AGENT_SYSTEM)
            self._last_llm_ms = round((time.perf_counter() - t0) * 1000, 1)
            logger.info(f"LLM verdict received in {self._last_llm_ms:.0f} ms")
            return self._normalise_plan(plan, room, level, risk_rooms)
        except Exception as e:
            self._last_llm_ms = None
            logger.error(f"LLM planning failed: {e}")
            return self._fallback_plan(room, level, features, risk_rooms)

    @staticmethod
    def _normalise_plan(raw, room, level, risk_rooms):
        """LLM returns a verdict; the response plan is derived here, never by the model."""
        a = raw.get("assessment") or {}
        is_fire = bool(a.get("is_fire"))
        conf = float(a.get("confidence", 0) or 0)
        act = is_fire and conf >= 0.7
        alert = raw.get("alert") or (raw.get("alerts") or [None])[0]
        return {
            "assessment": {
                "is_fire": is_fire,
                "confidence": conf,
                "reasoning": a.get("reasoning", ""),
            },
            "affected_room": room,
            "affected_level": level,
            "risk_rooms": [r.room for r in risk_rooms[:5]],
            "plan": {"doors": [{"room": room, "action": "unlock"}] if act else [], "evacuate": act},
            "alerts": [alert] if alert else [],
        }

    def _fallback_plan(self, room, level, features, risk_rooms):
        # LLM unreachable: apply the deterministic multi-sensor rule the server escalates
        # on anyway (and the edge has already acted on), and act on it with confidence.
        # Scaling confidence by the readings left a textbook fire (smoke 12 / CO 25) at
        # 0.25, below the 0.7 gate, so the origin room was doubted while the LLM was down.
        smoke = features.get("smoke", 0)
        co = features.get("co", 0)
        stale = features.get("stale") or []
        is_fire = (smoke >= TAKEOVER_FIRE_SMOKE and co >= TAKEOVER_FIRE_CO) or (
            "co" in stale and smoke >= 2 * TAKEOVER_FIRE_SMOKE
        )
        confidence = 0.9 if is_fire else 0.2
        return {
            "assessment": {
                "is_fire": is_fire,
                "confidence": confidence,
                "reasoning": "Fallback: rule-based (LLM unavailable); "
                f"smoke={smoke:.1f}>={TAKEOVER_FIRE_SMOKE:g} and co={co:.1f}>={TAKEOVER_FIRE_CO:g}"
                if is_fire
                else "Fallback: rule-based (LLM unavailable); multi-sensor rule not met",
            },
            "affected_room": room,
            "affected_level": level,
            "risk_rooms": [r.room for r in risk_rooms[:5]],
            "plan": {
                "doors": [{"room": room, "action": "unlock"}] if is_fire else [],
                "evacuate": is_fire,
            },
            "alerts": [
                {
                    "severity": "critical" if is_fire else "warning",
                    "title": f"{'Fire' if is_fire else 'Anomaly'} in {room}",
                    "message": f"Smoke={smoke:.1f}, CO={co:.1f} (fallback mode)",
                }
            ],
        }

    async def _verify(self, plan):
        issues = []
        assessment = plan.get("assessment", {})
        if assessment.get("is_fire") and assessment.get("confidence", 0) >= 0.7:
            plan_data = plan.get("plan", {})
            if not plan_data.get("doors"):
                issues.append("Fire detected but no doors unlocked")
        if issues:
            logger.warning(f"Verification issues: {issues}")
        return len(issues) == 0, issues

    # ------------------------------------------------------- visualisation
    async def _execute(self, plan, risk_rooms, evac_routes):
        assessment = plan.get("assessment", {})
        room = plan.get("affected_room", "")
        level = plan.get("affected_level", "") or self.room_levels.get(room, "")
        if not assessment.get("is_fire"):
            logger.info(f"No fire confirmed in {room}")
            return
        self._active_fire = True

        appearances = [{"level": level, "room": room, "color": "#ff0000", "brightness": 0.8}]
        for r in risk_rooms[:5]:
            appearances.append(
                {"level": r.level, "room": r.room, "color": "#ff8800", "brightness": 0.6}
            )
        if evac_routes:
            for route in evac_routes[:2]:
                if route.path_rooms:
                    exit_room = route.path_rooms[-1]
                    appearances.append(
                        {
                            "level": self.room_levels.get(exit_room, level),
                            "room": exit_room,
                            "color": "#00cc00",
                            "brightness": 0.7,
                        }
                    )
        seen: set[str] = set()
        known = []
        for a in appearances:
            if a["room"] in self.room_levels and a["level"] and a["room"] not in seen:
                seen.add(a["room"])
                known.append(a)
        appearances = known
        self._viz["appearance"][room] = appearances

        self._viz["effects"][room] = {
            "id": f"fire-{self._safe(room)}",
            "type": "fire",
            "label": f"Fire in {room}",
            "level": level,
            "room": room,
            "radius": 3.0,
            "intensity": assessment.get("confidence", 0.8),
        }

        alerts = plan.get("alerts", [])
        if alerts:
            bs_alerts = [
                {
                    "id": f"alert-{self._safe(room)}-{int(time.time())}",
                    "severity": a.get("severity", "warning"),
                    "title": a.get("title", "Fire alert"),
                    "message": a.get("message", ""),
                    "level": level,
                    "room": room,
                    "timestamp": datetime.now(UTC).isoformat(),
                }
                for a in alerts
            ]
            self._viz["alerts"][room] = bs_alerts

        try:
            coverage = []
            for r in risk_rooms[:5]:
                pos = self.spread._room_positions.get(r.room)
                if pos:
                    coverage.append(
                        {
                            "id": f"risk-{self._safe(r.room)}",
                            "type": "sphere",
                            "level": r.level,
                            "room": r.room,
                            "position": list(pos),
                            "radius": r.risk_score * 5.0,
                            "color": "#ff880088",
                        }
                    )
            self._viz["coverage"][room] = coverage
        except Exception as e:
            logger.warning(f"Failed to compute coverage: {e}")
        await self._push_viz()

        if evac_routes:
            best = evac_routes[0]
            if best.path_rooms and best.total_weight < float("inf"):
                logger.info(f"Displaying route: {' -> '.join(best.path_rooms)}")

        logger.info(
            f"FIRE RESPONSE executed for {room} | risk_rooms={[r.room for r in risk_rooms[:5]]}"
        )
        severity = "critical" if assessment.get("confidence", 0) >= 0.7 else "warning"
        await self._log_fire_event(room, level, severity, plan)

    # -------------------------------------------------------- visualisation state
    async def _push_viz(self):
        """Replace BuildSim's effect/tint/alert/coverage lists with the current incident state."""
        v = self._viz
        appearance, seen = [], set()
        for items in v["appearance"].values():
            for a in items:
                if a["room"] not in seen:
                    seen.add(a["room"])
                    appearance.append(a)
        payloads = [
            ("set_room_appearance", appearance),
            ("set_effects", list(v["effects"].values())),
            ("set_alerts", [a for items in v["alerts"].values() for a in items]),
            ("set_coverage", [c for items in v["coverage"].values() for c in items]),
        ]
        for name, payload in payloads:
            try:
                await getattr(self.bs, name)(payload)
            except Exception as e:
                logger.warning(f"BuildSim {name} failed: {e}")

    async def _clear_room_viz(self, room):
        if any(room in self._viz[k] for k in self._viz):
            for k in self._viz:
                self._viz[k].pop(room, None)
            await self._push_viz()

    async def _clear_all_viz(self):
        for k in self._viz:
            self._viz[k].clear()
        await self._push_viz()
        logger.info("BuildSim visualisation cleared")

    # -------------------------------------------------------------- storage
    async def _db(self):
        return await asyncpg.connect(
            host=self.db_config["host"],
            port=self.db_config["port"],
            user=self.db_config["user"],
            password=self.db_config["password"],
            database=self.db_config["database"],
        )

    async def _log_fire_event(self, room, level, severity, plan):
        if not self.db_config:
            return
        try:
            conn = await self._db()
            await conn.execute(
                """INSERT INTO fire_detection_sim.fire_events
                   (time, event_type, room, level, severity, details)
                   VALUES (NOW(), $1, $2, $3, $4, $5)""",
                "fire_detected" if plan.get("assessment", {}).get("is_fire") else "anomaly",
                room,
                level,
                severity,
                json.dumps(plan),
            )
            await conn.close()
        except Exception as e:
            logger.warning(f"Failed to log fire event: {e}")

    async def _log_override(self, room, level, override_type, reason, llm_ms=None):
        if not self.db_config:
            return
        try:
            conn = await self._db()
            await conn.execute(
                """INSERT INTO fire_detection_sim.llm_overrides
                   (time, room, level, override_type, reason, llm_ms)
                   VALUES (NOW(), $1, $2, $3, $4, $5)""",
                room,
                level,
                override_type,
                reason,
                llm_ms,
            )
            await conn.close()
        except Exception as e:
            logger.warning(f"Failed to log override: {e}")

    def _log_episode(self, room, features, plan, risk_rooms, evac_routes, verified, issues):
        self._episode_log.append(
            {
                "timestamp": time.time(),
                "room": room,
                "features": features,
                "plan": plan,
                "risk_rooms": [
                    {"room": r.room, "risk": r.risk_score, "dist": r.distance} for r in risk_rooms
                ],
                "evacuation": [
                    {"to": r.to_exit, "weight": r.total_weight, "safe": r.is_safe}
                    for r in evac_routes
                ],
                "verified": verified,
                "issues": issues,
            }
        )
