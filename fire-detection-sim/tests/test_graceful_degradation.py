"""Graceful degradation test.

Proves the system works when the LLM is unavailable:
- Agent falls back to rule-based decisions
- Fire is still detected and responded to
- Sprinklers activate, doors unlock
- The system doesn't crash
"""

import logging
import os

import pytest

from fds.agent.agent import FireAgent
from fds.buildsim.client import BuildSimClient
from fds.config import Config
from fds.models.detector import FireDetector
from fds.models.evacuation import EvacuationPlanner
from fds.models.spread import FireSpreadPredictor
from fds.physics.engine import PhysicsEngine
from fds.physics.sensor_gateway import SensorConfig, SensorGateway
from fds.secrets import get_secrets

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

skipci = pytest.mark.skipif(os.getenv("CI") == "true", reason="No BuildSim in CI")


@skipci
@pytest.mark.asyncio
async def test_fallback_agent_detects_and_responds():
    """Full system test with broken LLM — verifies fallback works."""
    config = Config.from_yaml("configs/config.yaml")
    secrets = get_secrets()
    config.database.password = secrets.get("db_password", "")

    room = "A109"
    level = "level0"

    # Physics engine with immediate fire
    engine = PhysicsEngine()
    engine.add_room(room, level)
    engine.start_fire(room)

    # Run physics for 40 steps to build up fire
    for _ in range(40):
        engine.step(dt=2.0)

    # Sensor gateway (read truth, degrade)
    gateway = SensorGateway(config, engine, interval=2.0)
    gateway.add_sensor(
        SensorConfig(
            sensor_id=f"{room}-smoke",
            equipment_id=f"test-sensors-{room}",
            room=room,
            level=level,
            sensor_type="smoke",
            unit="%obs/m",
            noise_sigma=0.3,
        )
    )
    gateway.add_sensor(
        SensorConfig(
            sensor_id=f"{room}-co",
            equipment_id=f"test-sensors-{room}",
            room=room,
            level=level,
            sensor_type="co",
            unit="ppm",
            noise_sigma=0.5,
        )
    )
    gateway.add_sensor(
        SensorConfig(
            sensor_id=f"{room}-temperature",
            equipment_id=f"test-sensors-{room}",
            room=room,
            level=level,
            sensor_type="temperature",
            unit="C",
            noise_sigma=0.15,
        )
    )

    # Detector
    detector = FireDetector()
    try:
        detector.load("models/detector.pkl")
    except FileNotFoundError:
        from fds.models.trainer import train_detector

        detector = train_detector()

    # Spread + evac (minimal)
    spread = FireSpreadPredictor(spread_radius=50.0)
    spread.load_graph({"nodes": [{"id": 1, "name": room, "x": 0, "y": 0}], "edges": []})

    evac = EvacuationPlanner()
    evac.load_graph({"nodes": [{"id": 1, "name": room, "x": 0, "y": 0}], "edges": []})

    async with BuildSimClient(config.buildsim.url) as bs:
        # Register sensors in BuildSim
        await gateway.register_all(bs)

        # Write fire readings to BuildSim
        for sensor in gateway.sensors:
            truth = gateway._read_truth(sensor)
            value = round(truth, 2)
            await bs.set_sensor_value(sensor.sensor_id, str(value))

        # Create agent with BROKEN LLM — forces fallback
        agent = FireAgent(
            bs=bs,
            detector=detector,
            spread_predictor=spread,
            evacuation_planner=evac,
            llm_url="https://192.168.68.67:99999",  # Deliberately broken
            llm_api_key="broken",
            model="broken",
            tick_interval=1.0,
        )

        # Feed readings to agent
        room_state = engine.get_room(room)
        agent.update_reading(room, "smoke", room_state.smoke)
        agent.update_reading(room, "co", room_state.co)
        agent.update_reading(room, "temperature", room_state.temperature)

        # Process the room — should use fallback
        await agent.process_room(room, level)

        # Verify fallback worked
        log = agent.get_episode_log()
        assert len(log) >= 1, "No episodes logged"

        episode = log[0]
        plan = episode["plan"]
        assessment = plan.get("assessment", {})

        # Fallback should detect fire (high smoke + CO)
        assert assessment.get("is_fire") is True, f"Fallback did not detect fire: {assessment}"
        assert "fallback" in assessment.get("reasoning", "").lower(), "Not using fallback"

        # Fallback should activate sprinklers
        sprinklers = plan.get("plan", {}).get("sprinklers", [])
        assert len(sprinklers) > 0, "No sprinklers in fallback plan"

        # Fallback should unlock doors
        doors = plan.get("plan", {}).get("doors", [])
        assert len(doors) > 0, "No doors in fallback plan"

        logger.info("GRACEFUL DEGRADATION TEST PASSED")
        logger.info(f"  Fire detected: {assessment.get('is_fire')}")
        logger.info(f"  Confidence: {assessment.get('confidence')}")
        logger.info(f"  Reasoning: {assessment.get('reasoning')}")
        logger.info(f"  Sprinklers: {sprinklers}")
        logger.info(f"  Doors: {doors}")

        # Cleanup
        try:
            await bs.delete_equipment(f"test-sensors-{room}")
        except Exception:
            pass


@skipci
@pytest.mark.asyncio
async def test_fallback_rejects_false_alarm():
    """Fallback correctly rejects false alarm (smoke only, no CO)."""
    config = Config.from_yaml("configs/config.yaml")

    detector = FireDetector()
    try:
        detector.load("models/detector.pkl")
    except FileNotFoundError:
        from fds.models.trainer import train_detector

        detector = train_detector()

    spread = FireSpreadPredictor()
    evac = EvacuationPlanner()

    async with BuildSimClient(config.buildsim.url) as bs:
        agent = FireAgent(
            bs=bs,
            detector=detector,
            spread_predictor=spread,
            evacuation_planner=evac,
            llm_url="https://192.168.68.67:99999",
            llm_api_key="broken",
            model="broken",
            tick_interval=1.0,
        )

        # Feed false alarm: smoke spike, no CO
        for _ in range(5):
            agent.update_reading("A109", "smoke", 8.0)
            agent.update_reading("A109", "co", 2.0)
            agent.update_reading("A109", "temperature", 21.0)

        await agent.process_room("A109", "level0")

        log = agent.get_episode_log()
        # Detector may or may not flag this — if it does, fallback should
        # classify as low confidence (smoke without CO)
        if log:
            plan = log[0]["plan"]
            confidence = plan.get("assessment", {}).get("confidence", 0)
            logger.info(f"False alarm confidence: {confidence}")
            # Low smoke + low CO = low confidence in fallback
            assert confidence < 0.7, f"Fallback confidence too high for false alarm: {confidence}"

        logger.info("FALSE ALARM REJECTION TEST PASSED")
