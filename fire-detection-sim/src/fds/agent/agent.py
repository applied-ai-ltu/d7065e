"""Autonomous fire detection and response agent.

Loop: detect -> plan (via LLM) -> verify -> act -> check outcomes.
"""

import asyncio
import json
import logging
import time
from collections import defaultdict
from datetime import datetime, timezone

from fds.agent.llm import LLMClient
from fds.agent.prompts import FIRE_AGENT_SYSTEM, VERIFICATION_SYSTEM
from fds.buildsim.client import BuildSimClient
from fds.models.detector import FireDetector

logger = logging.getLogger(__name__)


class FireAgent:

    def __init__(
        self,
        bs: BuildSimClient,
        detector: FireDetector,
        llm_url="https://192.168.68.67:30400",
        llm_api_key="",
        model="gemma4:12b",
        tick_interval=5.0,
    ):
        self.bs = bs
        self.detector = detector
        self.llm = LLMClient(base_url=llm_url, api_key=llm_api_key, model=model)
        self.tick_interval = tick_interval
        self._running = False
        self._sensor_history = defaultdict(lambda: defaultdict(list))
        self._window_size = 10
        self._episode_log = []
        self._active_fire = False
        self._last_plan = None

    def update_reading(self, room, sensor_type, value):
        """Called by the pipeline to feed sensor data to the agent."""
        history = self._sensor_history[room][sensor_type]
        history.append(value)
        if len(history) > self._window_size:
            history.pop(0)

    def _get_room_readings(self, room):
        readings = {}
        for sensor_type in ["smoke", "co", "temperature"]:
            values = self._sensor_history[room].get(sensor_type, [0.0])
            readings[sensor_type] = values if values else [0.0]
        return readings

    async def _detect(self, room):
        """Run anomaly detection on a room."""
        readings = self._get_room_readings(room)
        if not self.detector.is_fitted:
            return False, 0.0, {}
        is_fire, score, features = self.detector.predict(readings)
        return is_fire, score, features

    async def _plan(self, room, level, features):
        """Ask the LLM to generate a response plan."""
        prompt = f"""Current sensor readings for room {room} on {level}:
- Smoke: {features.get('smoke', 0):.1f} %obs/m
- CO: {features.get('co', 0):.1f} ppm
- Temperature: {features.get('temperature', 0):.1f} C
- Smoke rate of change: {features.get('smoke_rate', 0):.2f}
- CO rate of change: {features.get('co_rate', 0):.2f}
- Temperature rate of change: {features.get('temp_rate', 0):.2f}

The anomaly detector has flagged this as a potential fire.
Assess the situation and provide a response plan."""

        try:
            plan = await self.llm.generate_json(prompt, system=FIRE_AGENT_SYSTEM)
            logger.info(f"LLM plan: {json.dumps(plan, indent=2)}")
            return plan
        except Exception as e:
            logger.error(f"LLM planning failed: {e}")
            return self._fallback_plan(room, level, features)

    def _fallback_plan(self, room, level, features):
        """Rule-based fallback when LLM is unavailable."""
        logger.warning("Using fallback rule-based plan")
        smoke = features.get("smoke", 0)
        co = features.get("co", 0)

        is_fire = smoke > 10 and co > 20
        confidence = min(1.0, (smoke / 50 + co / 100) / 2)

        return {
            "assessment": {
                "is_fire": is_fire,
                "confidence": confidence,
                "reasoning": "Fallback: rule-based assessment (LLM unavailable)"
            },
            "affected_room": room,
            "affected_level": level,
            "risk_rooms": [],
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
        """Verify the plan against safety constraints."""
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

    async def _execute(self, plan):
        """Execute the response plan via BuildSim."""
        assessment = plan.get("assessment", {})
        plan_data = plan.get("plan", {})

        room = plan.get("affected_room", "")
        level = plan.get("affected_level", "")

        if assessment.get("is_fire"):
            self._active_fire = True

            # Highlight affected room red
            appearances = [{"level": level, "room": room, "color": "#ff0000", "brightness": 0.8}]

            # Highlight risk rooms orange
            for risk_room in plan.get("risk_rooms", []):
                appearances.append({"level": level, "room": risk_room, "color": "#ff8800", "brightness": 0.6})

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

            logger.info(f"FIRE RESPONSE executed for {room}")
        else:
            logger.info(f"No fire confirmed in {room}, monitoring continues")

    def _log_episode(self, room, features, plan, verified, issues):
        """Log episode for GEPA evolution."""
        self._episode_log.append({
            "timestamp": time.time(),
            "room": room,
            "features": features,
            "plan": plan,
            "verified": verified,
            "issues": issues,
        })

    async def process_room(self, room, level):
        """Run one detection-plan-verify-act cycle for a room."""
        is_fire, score, features = await self._detect(room)

        if not is_fire:
            return

        logger.info(f"ANOMALY detected in {room}: score={score:.3f}, features={features}")

        plan = await self._plan(room, level, features)
        verified, issues = await self._verify(plan)

        if not verified:
            logger.warning(f"Plan verification failed: {issues}")

        await self._execute(plan)
        self._log_episode(room, features, plan, verified, issues)
        self._last_plan = plan

    async def run(self, rooms):
        """Main agent loop.

        rooms: list of (room, level) tuples to monitor.
        """
        self._running = True
        logger.info(f"Agent started, monitoring {len(rooms)} rooms")

        while self._running:
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
