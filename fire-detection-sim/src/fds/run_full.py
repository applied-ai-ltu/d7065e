"""Run the complete fire detection system — all 3 floors.

Architecture (matches reference):
- Physics engine: computes ground truth for 970 rooms across 3 floors
- Sensor gateway: degrades truth, writes to BuildSim + MQTT
- Pipeline consumer: MQTT -> TimescaleDB
- Agent: reads observations from BuildSim, decides, commands actuators
- Actuator processes: travel toward commands, report state
- Fire response is building-wide: doors unlock on ALL floors
"""

# Initialize tracing BEFORE any other imports that touch OpenTelemetry
from fds.tracing import setup_phoenix
_tracer_provider = setup_phoenix("d7065e")

import asyncio
import json
import logging
import math
import os
import time

from fds.buildsim.client import BuildSimClient
from fds.config import Config
from fds.api.control import app as control_app, sim
import uvicorn
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

LEVELS = ["level0", "level1", "level2"]


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


async def physics_loop(engine, bs, occupancy_sim, dt=2.0):
    """Run the physics engine. Fire triggered externally via control API."""
    sim_hour = 9.0

    while True:
        try:
            await engine.sync_actuators_from_buildsim(bs)
        except Exception:
            pass

        sim_hour += (dt / 60.0)
        if sim_hour >= 24:
            sim_hour = 7.0

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

    # --- Physics Engine (all 3 floors) ---
    engine = PhysicsEngine(outdoor_temp=-5.0)
    all_room_types = {}
    all_rooms_by_level = {}
    combined_graph = {"nodes": [], "edges": []}

    async with BuildSimClient(config.buildsim.url) as bs:
        for level in LEVELS:
            graph = await bs.get_graph(level=level)
            floor_data = await bs.get_floor(level)
            room_types = {r["name"]: r.get("type", "room") for r in floor_data.get("rooms", [])}
            all_room_types.update(room_types)

            level_rooms = []
            node_positions = {}
            seen_names = set()
            for node in graph["nodes"]:
                name = node["name"]
                if name in seen_names:
                    continue  # Skip duplicate room names (e.g. corridor segments)
                seen_names.add(name)
                engine.add_room(name, level)
                node_positions[name] = (node["x"], node["y"])
                level_rooms.append(name)
                combined_graph["nodes"].append(node)

            combined_graph["edges"].extend(graph.get("edges", []))

            # Set neighbors based on spatial proximity (same floor)
            for name, pos in node_positions.items():
                neighbors = []
                for other_name, other_pos in node_positions.items():
                    if other_name == name:
                        continue
                    dist = math.sqrt((pos[0] - other_pos[0])**2 + (pos[1] - other_pos[1])**2)
                    if dist < 50:
                        neighbors.append((other_name, dist))
                engine.set_neighbors(name, neighbors)

            all_rooms_by_level[level] = level_rooms
            logger.info(f"{level}: {len(level_rooms)} rooms loaded")

        # Cross-floor neighbors via stairwells
        try:
            cross_edges = await bs.get_cross_floor_edges()
            for edge in cross_edges:
                from_room = edge.get("from_name", "")
                to_room = edge.get("to_name", "")
                if from_room in engine.rooms and to_room in engine.rooms:
                    # Stairwell connection — higher distance for cross-floor spread
                    existing = engine.neighbors.get(from_room, [])
                    existing.append((to_room, 80.0))
                    engine.set_neighbors(from_room, existing)
                    existing_to = engine.neighbors.get(to_room, [])
                    existing_to.append((from_room, 80.0))
                    engine.set_neighbors(to_room, existing_to)
            logger.info(f"Cross-floor connections: {len(cross_edges)} stairwell links")
        except Exception as e:
            logger.warning(f"Cross-floor edges failed: {e}")

        total_rooms = len(engine.rooms)
        logger.info(f"Physics engine: {total_rooms} rooms across {len(LEVELS)} floors")

        # Wire engine into control API
        sim.engine = engine
        sim.running = True
        sim.start_time = time.time()
        control_config = uvicorn.Config(control_app, host="0.0.0.0", port=8089, log_level="warning")
        control_server = uvicorn.Server(control_config)

        # Occupancy simulator
        occupancy_sim = OccupancySimulator(room_types=all_room_types)

        # --- Sensor Gateway (all rooms, all floors) ---
        gateway = SensorGateway(config, engine, interval=5.0)
        equipment_ids = []
        all_monitored = []

        for level in LEVELS:
            for room_name in all_rooms_by_level[level]:
                eq_id = f"sensors-{level}-{room_name}"
                equipment_ids.append(eq_id)
                all_monitored.append((room_name, level))

                for stype, unit, noise, lag, drift, stuck in [
                    ("smoke", "%obs/m", 0.3, 2.0, 0.001, 0.005),
                    ("co", "ppm", 0.5, 3.0, 0.002, 0.005),
                    ("temperature", "C", 0.15, 1.0, 0.0005, 0.002),
                ]:
                    gateway.add_sensor(SensorConfig(
                        sensor_id=f"{level}-{room_name}-{stype}",
                        equipment_id=eq_id,
                        room=room_name,
                        level=level,
                        sensor_type=stype,
                        unit=unit,
                        noise_sigma=noise,
                        lag_seconds=lag,
                        drift_rate=drift,
                        stuck_probability=stuck,
                    ))

        logger.info(f"Sensor gateway: {len(gateway.sensors)} sensors across {len(LEVELS)} floors")

        # --- Actuators ---
        # Register actuators in BuildSim but don't run individual processes.
        # Agent commands actuators via BuildSim. Physics engine reads states.
        # This scales to 970 rooms without 1100+ HTTP-polling processes.
        actuator_count = 0
        for level in LEVELS:
            for room_name in all_rooms_by_level[level]:
                spr_eq = f"sprinkler-{level}-{room_name}"
                equipment_ids.append(spr_eq)
                try:
                    await bs.get_equipment(spr_eq)
                except Exception:
                    try:
                        await bs.create_equipment({
                        "id": spr_eq,
                        "name": f"Sprinkler ({room_name})",
                        "type": "actuator", "category": "fire_response",
                        "level": level, "room": room_name, "status": "running",
                        "sensors": [],
                        "actuators": [{"id": f"{level}-{room_name}-sprinkler", "name": "Sprinkler",
                                       "type": "sprinkler", "state": "off"}],
                        })
                    except Exception as e:
                        logger.debug(f"Sprinkler reg skip: {e}")
                actuator_count += 1

                if all_room_types.get(room_name) == "corridor":
                    door_eq = f"door-{level}-{room_name}"
                    equipment_ids.append(door_eq)
                    try:
                        await bs.get_equipment(door_eq)
                    except Exception:
                        try:
                            await bs.create_equipment({
                            "id": door_eq,
                            "name": f"Fire door ({room_name})",
                            "type": "actuator", "category": "fire_response",
                            "level": level, "room": room_name, "status": "running",
                            "sensors": [],
                            "actuators": [{"id": f"{level}-{room_name}-door", "name": "Fire door",
                                           "type": "fire_door", "state": "closed_locked"}],
                            })
                        except Exception as e:
                            logger.debug(f"Door reg skip: {e}")
                    actuator_count += 1

        logger.info(f"Actuators: {actuator_count} registered in BuildSim")

        # --- Pipeline Consumer ---
        consumer = PipelineConsumer(config)

        # --- Detector ---
        detector = FireDetector()
        try:
            detector.load("models/detector.pkl")
        except FileNotFoundError:
            from fds.models.trainer import train_detector
            detector = train_detector()

        # --- Spread + Evacuation (combined graph, all floors) ---
        spread = FireSpreadPredictor(spread_radius=50.0)
        spread.load_graph(combined_graph)

        evac = EvacuationPlanner()
        evac.load_graph(combined_graph)

        # Exits: edge corridors per floor + stairwell rooms
        exit_names = []
        for level in LEVELS:
            level_corridors = [
                n for n in combined_graph["nodes"]
                if n["name"] in all_rooms_by_level.get(level, [])
                and all_room_types.get(n["name"]) == "corridor"
            ]
            level_corridors.sort(key=lambda n: n["x"])
            if level_corridors:
                exit_names.extend([n["name"] for n in level_corridors[:2]])
                exit_names.extend([n["name"] for n in level_corridors[-2:]])

        try:
            cross_edges = await bs.get_cross_floor_edges()
            for edge in cross_edges:
                stair = edge.get("from_name", "")
                if stair and stair not in exit_names:
                    exit_names.append(stair)
                stair_to = edge.get("to_name", "")
                if stair_to and stair_to not in exit_names:
                    exit_names.append(stair_to)
        except Exception:
            pass

        exit_names = list(set(exit_names))
        evac.set_exits(exit_names)
        logger.info(f"Exits: {len(exit_names)} ({exit_names[:5]}...)")

        # --- Agent (reads from BuildSim, building-wide response) ---
        agent = FireAgent(
            bs=bs, detector=detector,
            spread_predictor=spread,
            evacuation_planner=evac,
            llm_url=config.llm.base_url,
            llm_api_key=secrets.get("litellm_api_key", ""),
            model=config.llm.model,
            tick_interval=10.0,
            db_config={
                "host": config.database.host,
                "port": config.database.port,
                "user": config.database.user,
                "password": config.database.password,
                "database": config.database.database,
            },
        )

        print("=" * 60)
        print("Fire Detection System — Full Building")
        print("=" * 60)
        print(f"Floors: {LEVELS}")
        print(f"Rooms: {total_rooms} ({sum(len(v) for v in all_rooms_by_level.values())} monitored)")
        print(f"Sensors: {len(gateway.sensors)} (smoke + CO + temperature per room)")
        print(f"Actuators: {actuator_count} registered in BuildSim")
        print(f"Exits: {len(exit_names)}")
        print(f"Agent model: {config.llm.model} (tick=10s)")
        print(f"Control API: http://localhost:8089/docs")
        print(f"BuildSim: {config.buildsim.url}")
        print(f"Press Ctrl+C to stop")
        print("=" * 60)

        tasks = [
            asyncio.create_task(control_server.serve()),
            asyncio.create_task(physics_loop(engine, bs, occupancy_sim)),
            asyncio.create_task(gateway.run(bs)),
            asyncio.create_task(consumer.run()),
            asyncio.create_task(agent.run(all_monitored)),
        ]

        try:
            await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            pass
        finally:
            gateway.stop()
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
