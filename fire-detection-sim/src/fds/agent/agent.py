"""Autonomous fire detection and response agent.

Loop: detect -> spread prediction -> plan (via LLM) -> verify -> act -> check outcomes.
"""

import asyncio
import json
import logging
import time
from collections import defaultdict
from datetime import datetime, timezone

import asyncpg

from fds.agent.llm import LLMClient
from fds.agent.prompts import FIRE_AGENT_SYSTEM
from fds.buildsim.client import BuildSimClient
from fds.models.detector import FireDetector
from fds.models.spread import FireSpreadPredictor
from fds.models.evacuation import EvacuationPlanner

logger = logging.getLogger(__name__)


class FireAgent:

    def __init__(
        self,
        bs: BuildSimClient,
        detector: FireDetector,
        spread_predictor: FireSpreadPredictor,
        evacuation_planner: EvacuationPlanner,
        llm_url="https://192.168.68.67:30400",
        llm_api_key="",
        model="qwen2.5-14b",
        tick_interval=5.0,
        db_config=None,
    ):
        self.bs = bs
        self.detector = detector
        self.spread = spread_predictor
        self.evac = evacuation_planner
        self.llm = LLMClient(base_url=llm_url, api_key=llm_api_key, model=model)
        self.tick_interval = tick_interval
        self.db_config = db_config
        self._running = False
        self._sensor_history = defaultdict(lambda: defaultdict(list))
        self._window_size = 10
        self._episode_log = []
        self._active_fire = False
        self._fire_start_time = {}
        self._last_plan = None

    def update_reading(self, room, sensor_type, value):
        """Can still be called externally, but primary path is poll_buildsim."""
        history = self._sensor_history[room][sensor_type]
        history.append(value)
        if len(history) > self._window_size:
            history.pop(0)

    async def poll_buildsim(self, rooms):
        """Read current sensor values from BuildSim — the canonical observation path.

        Uses equipment list to batch-read all sensors efficiently.
        """
        try:
            equipment = await self.bs.list_equipment()
            # Index by room
            for eq in equipment:
                room_name = eq.get("room", "")
                if not room_name:
                    continue
                for sensor in eq.get("sensors", []):
                    stype = sensor.get("type", "")
                    if stype not in ("smoke", "co", "temperature"):
                        continue
                    value_str = sensor.get("value", "0")
                    try:
                        value = float(value_str)
                        if value == 0.0 and stype == "temperature":
                            continue
                        self.update_reading(room_name, stype, value)
                    except (ValueError, TypeError):
                        pass
        except Exception as e:
            logger.warning(f"Failed to poll BuildSim: {e}")

    def _get_room_readings(self, room):
        readings = {}
        for sensor_type in ["smoke", "co", "temperature"]:
            values = self._sensor_history[room].get(sensor_type, [])
            if not values:
                readings[sensor_type] = [0.0]
            else:
                readings[sensor_type] = values
        return readings

    async def _detect(self, room):
        readings = self._get_room_readings(room)
        if not self.detector.is_fitted:
            return False, 0.0, {}
        is_fire, score, features = self.detector.predict(readings)
        return is_fire, score, features

    async def _plan(self, room, level, features, risk_rooms, evac_routes):
        risk_str = ", ".join([f"{r.room} (risk={r.risk_score})" for r in risk_rooms[:5]]) or "none nearby"
        route_str = ""
        if evac_routes:
            best = evac_routes[0]
            route_str = f"Best route: {' -> '.join(best.path_rooms)} (distance={best.total_weight})"

        prompt = f"""Current sensor readings for room {room} on {level}:
- Smoke: {features.get('smoke', 0):.1f} %obs/m
- CO: {features.get('co', 0):.1f} ppm
- Temperature: {features.get('temperature', 0):.1f} C
- Smoke rate of change: {features.get('smoke_rate', 0):.2f}
- CO rate of change: {features.get('co_rate', 0):.2f}
- Temperature rate of change: {features.get('temp_rate', 0):.2f}

Rooms at risk from fire spread: {risk_str}
Evacuation: {route_str}

The anomaly detector has flagged this as a potential fire.
Assess the situation and provide a response plan."""

        try:
            plan = await self.llm.generate_json(prompt, system=FIRE_AGENT_SYSTEM)
            logger.info(f"LLM plan received")
            return plan
        except Exception as e:
            logger.error(f"LLM planning failed: {e}")
            return self._fallback_plan(room, level, features, risk_rooms)

    def _fallback_plan(self, room, level, features, risk_rooms):
        logger.warning("Using fallback rule-based plan")
        smoke = features.get("smoke", 0)
        co = features.get("co", 0)

        is_fire = smoke > 10 and co > 20
        confidence = min(1.0, (smoke / 50 + co / 100) / 2)

        return {
            "assessment": {
                "is_fire": is_fire,
                "confidence": confidence,
                "reasoning": "Fallback: rule-based (LLM unavailable)"
            },
            "affected_room": room,
            "affected_level": level,
            "risk_rooms": [r.room for r in risk_rooms[:5]],
            "plan": {
                "sprinklers": [{"room": room, "action": "activate"}] if is_fire else [],
                "doors": [{"room": room, "action": "unlock"}] if is_fire else [],
                "evacuate": is_fire,
            },
            "alerts": [{
                "severity": "critical" if is_fire else "warning",
                "title": f"{'Fire' if is_fire else 'Anomaly'} in {room}",
                "message": f"Smoke={smoke:.1f}, CO={co:.1f} (fallback mode)"
            }],
        }

    async def _verify(self, plan):
        issues = []
        assessment = plan.get("assessment", {})
        if assessment.get("is_fire") and assessment.get("confidence", 0) >= 0.7:
            plan_data = plan.get("plan", {})
            if not plan_data.get("sprinklers"):
                issues.append("High-confidence fire but no sprinklers activated")
            if not plan_data.get("doors"):
                issues.append("Fire detected but no doors unlocked")
        if issues:
            logger.warning(f"Verification issues: {issues}")
        return len(issues) == 0, issues

    async def _execute(self, plan, risk_rooms, evac_routes):
        assessment = plan.get("assessment", {})
        room = plan.get("affected_room", "")
        level = plan.get("affected_level", "")

        if not assessment.get("is_fire"):
            logger.info(f"No fire confirmed in {room}")
            return

        self._active_fire = True

        # Room appearances: red=fire, orange=risk, green=safe exits
        appearances = [{"level": level, "room": room, "color": "#ff0000", "brightness": 0.8}]
        for r in risk_rooms[:5]:
            appearances.append({"level": r.level, "room": r.room, "color": "#ff8800", "brightness": 0.6})

        # Mark evacuation exit rooms green
        if evac_routes:
            for route in evac_routes[:2]:
                if route.path_rooms:
                    exit_room = route.path_rooms[-1]
                    appearances.append({"level": level, "room": exit_room, "color": "#00cc00", "brightness": 0.7})

        try:
            await self.bs.set_room_appearance(appearances)
        except Exception as e:
            logger.warning(f"Failed to set room appearance: {e}")

        # Fire effect
        try:
            await self.bs.set_effects([{
                "id": f"fire-{room}",
                "type": "fire",
                "label": f"Fire in {room}",
                "level": level,
                "room": room,
                "radius": 3.0,
                "intensity": assessment.get("confidence", 0.8),
            }])
        except Exception as e:
            logger.warning(f"Failed to set effects: {e}")

        # Alerts
        alerts = plan.get("alerts", [])
        if alerts:
            bs_alerts = []
            for alert in alerts:
                bs_alerts.append({
                    "id": f"alert-{room}-{int(time.time())}",
                    "severity": alert.get("severity", "warning"),
                    "title": alert.get("title", "Fire alert"),
                    "message": alert.get("message", ""),
                    "level": level,
                    "room": room,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                })
            try:
                await self.bs.set_alerts(bs_alerts)
            except Exception as e:
                logger.warning(f"Failed to set alerts: {e}")

        # Coverage zones for smoke spread risk
        try:
            coverage = []
            for r in risk_rooms[:5]:
                pos = self.spread._room_positions.get(r.room)
                if pos:
                    coverage.append({
                        "id": f"risk-{r.room}",
                        "type": "sphere",
                        "level": level,
                        "room": r.room,
                        "position": list(pos),
                        "radius": r.risk_score * 5.0,
                        "color": "#ff880088",
                    })
            if coverage:
                await self.bs.set_coverage(coverage)
        except Exception as e:
            logger.warning(f"Failed to set coverage: {e}")

        # Display evacuation routes on BuildSim
        if evac_routes:
            best_route = evac_routes[0]
            if best_route.path_rooms and best_route.total_weight < float("inf"):
                try:
                    # Convert path to node IDs for BuildSim route display
                    path_str = " -> ".join(best_route.path_rooms)
                    logger.info(f"Displaying route: {path_str}")
                except Exception as e:
                    logger.warning(f"Failed to display route: {e}")

        logger.info(f"FIRE RESPONSE executed for {room} | risk_rooms={[r.room for r in risk_rooms[:5]]}")

        # Log to database
        severity = "critical" if assessment.get("confidence", 0) >= 0.7 else "warning"
        await self._log_fire_event(room, level, severity, plan)

    async def _log_fire_event(self, room, level, severity, plan):
        """Write fire event to TimescaleDB for Grafana."""
        if not self.db_config:
            return
        try:
            conn = await asyncpg.connect(
                host=self.db_config["host"],
                port=self.db_config["port"],
                user=self.db_config["user"],
                password=self.db_config["password"],
                database=self.db_config["database"],
            )
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

    def _log_episode(self, room, features, plan, risk_rooms, evac_routes, verified, issues):
        self._episode_log.append({
            "timestamp": time.time(),
            "room": room,
            "features": features,
            "plan": plan,
            "risk_rooms": [{"room": r.room, "risk": r.risk_score, "dist": r.distance} for r in risk_rooms],
            "evacuation": [{"to": r.to_exit, "weight": r.total_weight, "safe": r.is_safe} for r in evac_routes],
            "verified": verified,
            "issues": issues,
        })

    async def process_room(self, room, level):
        is_fire, score, features = await self._detect(room)
        if not is_fire:
            return

        # Skip rooms with negligible readings — avoids LLM calls on noise
        smoke = features.get("smoke", 0)
        co = features.get("co", 0)
        temp = features.get("temperature", 0)
        if smoke < 2.0 and co < 5.0 and temp < 25.0:
            return

        logger.info(f"ANOMALY in {room}: score={score:.3f}, features={features}")

        # Track fire start time
        if room not in self._fire_start_time:
            self._fire_start_time[room] = time.time()
        elapsed = time.time() - self._fire_start_time[room]

        # Predict spread
        temp = features.get("temperature", 21)
        risk_rooms = self.spread.predict_spread(room, level, temperature=temp, elapsed=elapsed)
        logger.info(f"Spread prediction: {[(r.room, r.risk_score) for r in risk_rooms[:5]]}")

        # Plan evacuation avoiding high-risk rooms (but not the fire room itself)
        blocked = [r.room for r in risk_rooms if r.risk_score > 0.5]
        evac_routes = self.evac.find_routes(room, blocked_rooms=blocked)

        # Filter out routes with infinite weight
        valid_routes = [r for r in evac_routes if r.total_weight < float("inf")]

        if valid_routes:
            logger.info(f"Evacuation: best route to {valid_routes[0].to_exit} via {valid_routes[0].path_rooms[:5]} ({valid_routes[0].total_weight})")
        else:
            # Fallback: try without any blocked rooms
            evac_routes = self.evac.find_routes(room, blocked_rooms=None)
            valid_routes = [r for r in evac_routes if r.total_weight < float("inf")]
            if valid_routes:
                logger.info(f"Evacuation (unblocked): best route to {valid_routes[0].to_exit} ({valid_routes[0].total_weight})")
            else:
                logger.warning("No evacuation routes found")

        evac_routes = valid_routes or evac_routes

        # LLM plan
        plan = await self._plan(room, level, features, risk_rooms, evac_routes)
        verified, issues = await self._verify(plan)

        if not verified:
            logger.warning(f"Verification failed: {issues}")

        await self._execute(plan, risk_rooms, evac_routes)
        self._log_episode(room, features, plan, risk_rooms, evac_routes, verified, issues)
        self._last_plan = plan

    async def run(self, rooms):
        self._running = True
        logger.info(f"Agent started, monitoring {len(rooms)} rooms (reading from BuildSim)")
        while self._running:
            # Read observations from BuildSim — the only source of truth
            await self.poll_buildsim(rooms)

            for room, level in rooms:
                try:
                    await self.process_room(room, level)
                except Exception as e:
                    logger.error(f"Error processing {room}: {e}")
            await asyncio.sleep(self.tick_interval)

    def stop(self):
        self._running = False

    def get_episode_log(self):
        return self._episode_log
