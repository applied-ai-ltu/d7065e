"""Run the complete fire detection system.

Architecture (matches reference):
- Physics engine: computes ground truth
- Sensor gateway: degrades truth, writes to BuildSim + MQTT
- Pipeline consumer: MQTT -> TimescaleDB
- Agent: reads observations from BuildSim, decides, commands actuators
- Actuator processes: travel toward commands, report state
"""

# Initialize tracing BEFORE any other imports that touch OpenTelemetry
from fds.tracing import setup_phoenix
_tracer_provider = setup_phoenix("d7065e")

import asyncio
import json
import logging
import os
import time

from fds.buildsim.client import BuildSimClient
from fds.config import Config
from fds.secrets import get_secrets
from fds.physics.engine import PhysicsEngine
from fds.physics.sensor_gateway import SensorGateway, SensorConfig
from fds.physics.actuator_process import ActuatorProcess, ActuatorConfig
from fds.physics.occupancy import OccupancySimulator
from fds.models.detector import FireDetector
from fds.models.spread import FireSpreadPredictor
from fds.models.evacuation import EvacuationPlanner
from fds.agent.agent import FireAgent
from fds.pipeline.schema import init_database
from fds.pipeline.consumer import PipelineConsumer

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


async def cleanup(bs, equipment_ids):
    try:
        await bs.set_room_appearance([])
        await bs.set_effects([])
        await bs.set_alerts([])
        await bs.set_coverage([])
        for eid in equipment_ids:
            try:
                await bs.delete_equipment(eid)
            except Exception:
                pass
        logger.info("BuildSim cleaned up")
    except Exception as e:
        logger.warning(f"Cleanup error: {e}")


async def physics_loop(engine, bs, fire_room, fire_start_delay, occupancy_sim, dt=2.0):
    """Run the physics engine with a scheduled fire event.

    Reads actuator states from BuildSim each tick (closing the feedback loop).
    Updates occupancy per room based on time-of-day schedule.
    """
    start = time.time()
    fire_started = False
    sim_hour = 9.0  # Start simulation at 9am (busy building)

    while True:
        elapsed = time.time() - start

        if not fire_started and elapsed >= fire_start_delay:
            engine.start_fire(fire_room)
            fire_started = True

        # Read actuator states from BuildSim
        try:
            await engine.sync_actuators_from_buildsim(bs)
        except Exception:
            pass

        # Update occupancy (advance sim clock ~1 min per real second)
        sim_hour += (dt / 60.0)
        if sim_hour >= 24:
            sim_hour = 7.0  # Reset to morning

        for room_name in engine.rooms:
            occ = occupancy_sim.get_occupancy(room_name, sim_hour=sim_hour)
            engine.set_occupancy(room_name, occ)

        engine.step(dt)
        await asyncio.sleep(dt)


async def main():
    config = Config.from_yaml("configs/config.yaml")
    secrets = get_secrets()
    config.database.password = secrets.get("db_password", "")

    await init_database(
        config.database.host, config.database.port,
        config.database.user, config.database.password,
        config.database.database,
    )

    fire_start = 60.0
    room = "A109"
    level = "level0"
    monitored_rooms = ["A109", "A108", "A110", "A107", "A111"]

    # --- Physics Engine ---
    engine = PhysicsEngine(outdoor_temp=-5.0)

    async with BuildSimClient(config.buildsim.url) as bs:
        # Load building graph to get rooms and neighbors
        graph = await bs.get_graph(level=level)
        floor_data = await bs.get_floor(level)
        room_types = {r["name"]: r.get("type", "room") for r in floor_data.get("rooms", [])}

        # Add rooms to physics engine
        import math
        node_positions = {}
        for node in graph["nodes"]:
            engine.add_room(node["name"], level)
            node_positions[node["name"]] = (node["x"], node["y"])

        # Set neighbors based on spatial proximity
        for name, pos in node_positions.items():
            neighbors = []
            for other_name, other_pos in node_positions.items():
                if other_name == name:
                    continue
                dist = math.sqrt((pos[0] - other_pos[0])**2 + (pos[1] - other_pos[1])**2)
                if dist < 50:
                    neighbors.append((other_name, dist))
            engine.set_neighbors(name, neighbors)

        logger.info(f"Physics engine: {len(engine.rooms)} rooms loaded")

        # Occupancy simulator
        occupancy_sim = OccupancySimulator(room_types=room_types)

        # --- Sensor Gateway ---
        gateway = SensorGateway(config, engine, interval=2.0)

        # Register sensors for the target room
        equipment_ids = []
        for r in monitored_rooms:
            eq_id = f"sensors-{r}"
            equipment_ids.append(eq_id)
            for stype, unit, noise, lag, drift, stuck in [
                ("smoke", "%obs/m", 0.3, 2.0, 0.001, 0.005),
                ("co", "ppm", 0.5, 3.0, 0.002, 0.005),
                ("temperature", "C", 0.15, 1.0, 0.0005, 0.002),
            ]:
                gateway.add_sensor(SensorConfig(
                    sensor_id=f"{r}-{stype}",
                    equipment_id=eq_id,
                    room=r,
                    level=level,
                    sensor_type=stype,
                    unit=unit,
                    noise_sigma=noise,
                    lag_seconds=lag,
                    drift_rate=drift,
                    stuck_probability=stuck,
                ))

        # --- Actuator Processes ---
        actuators = []
        for r in monitored_rooms:
            spr_eq = f"sprinkler-{r}"
            door_eq = f"door-{r}"
            equipment_ids.extend([spr_eq, door_eq])
            actuators.append(ActuatorProcess(ActuatorConfig(
                actuator_id=f"{r}-sprinkler", equipment_id=spr_eq,
                room=r, level=level, actuator_type="sprinkler",
                travel_rate=0.5, stuck_probability=0.005,
            )))
            actuators.append(ActuatorProcess(ActuatorConfig(
                actuator_id=f"{r}-door", equipment_id=door_eq,
                room=r, level=level, actuator_type="fire_door",
                travel_rate=0.3, stuck_probability=0.002,
            )))

        # --- Pipeline Consumer ---
        consumer = PipelineConsumer(config)

        # --- Detector ---
        detector = FireDetector()
        try:
            detector.load("models/detector.pkl")
        except FileNotFoundError:
            from fds.models.trainer import train_detector
            detector = train_detector()

        # --- Spread + Evacuation ---
        spread = FireSpreadPredictor(spread_radius=50.0)
        spread.load_graph(graph)

        evac = EvacuationPlanner()
        evac.load_graph(graph)

        corridor_nodes = [n for n in graph["nodes"] if room_types.get(n["name"]) == "corridor"]
        corridor_nodes.sort(key=lambda n: n["x"])
        exit_names = []
        if corridor_nodes:
            exit_names.extend([n["name"] for n in corridor_nodes[:2]])
            exit_names.extend([n["name"] for n in corridor_nodes[-2:]])
        try:
            cross_edges = await bs.get_cross_floor_edges()
            for edge in cross_edges:
                if edge.get("from_level", "").endswith(level):
                    stair = edge.get("from_name", "")
                    if stair and stair not in exit_names:
                        exit_names.append(stair)
        except Exception:
            pass
        exit_names = list(set(exit_names))
        evac.set_exits(exit_names)

        # --- Agent (reads from BuildSim, not MQTT) ---
        agent = FireAgent(
            bs=bs, detector=detector,
            spread_predictor=spread,
            evacuation_planner=evac,
            llm_url=config.llm.base_url,
            llm_api_key=secrets.get("litellm_api_key", ""),
            model=config.llm.model,
            tick_interval=5.0,
            db_config={
                "host": config.database.host,
                "port": config.database.port,
                "user": config.database.user,
                "password": config.database.password,
                "database": config.database.database,
            },
        )

        print("=" * 60)
        print("Fire Detection System — Reference Architecture")
        print("=" * 60)
        print(f"Physics engine: {len(engine.rooms)} rooms")
        print(f"Sensor gateway: {len(gateway.sensors)} sensors (noise + lag + drift + stuck)")
        print(f"Fire room: {room} | Fire starts at: t={fire_start}s")
        print(f"Agent model: {config.llm.model}")
        print(f"Exits: {exit_names[:5]}")
        print(f"Occupancy: weekday schedule starting at 9:00")
        print(f"BuildSim: {config.buildsim.url}")
        print(f"Press Ctrl+C to stop")
        print("=" * 60)

        tasks = [
            asyncio.create_task(physics_loop(engine, bs, room, fire_start, occupancy_sim)),
            asyncio.create_task(gateway.run(bs)),
            *[asyncio.create_task(a.run(bs)) for a in actuators],
            asyncio.create_task(consumer.run()),
            asyncio.create_task(agent.run([(r, level) for r in monitored_rooms])),
        ]

        try:
            await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            pass
        finally:
            gateway.stop()
            for a in actuators:
                a.stop()
            consumer.stop()
            agent.stop()

            await cleanup(bs, equipment_ids)

            log = agent.get_episode_log()
            if log:
                os.makedirs("logs", exist_ok=True)
                with open("logs/episode_log.json", "w") as f:
                    json.dump(log, f, indent=2)
                logger.info(f"Saved {len(log)} episodes")

            print("Shutdown complete.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Stopped.")
