"""Run the complete fire detection system."""

import asyncio
import json
import logging
import os
import time

import paho.mqtt.client as mqtt

from fds.buildsim.client import BuildSimClient
from fds.config import Config
from fds.secrets import get_secrets
from fds.sensors.smoke import SmokeSensor
from fds.sensors.co import COSensor
from fds.sensors.temperature import TemperatureSensor
from fds.models.detector import FireDetector
from fds.models.spread import FireSpreadPredictor
from fds.models.evacuation import EvacuationPlanner
from fds.agent.agent import FireAgent
from fds.pipeline.schema import init_database
from fds.pipeline.consumer import PipelineConsumer

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


async def cleanup(bs, sensors):
    try:
        await bs.set_room_appearance([])
        await bs.set_effects([])
        await bs.set_alerts([])
        await bs.set_coverage([])
        for sensor in sensors:
            try:
                await bs.delete_equipment(sensor.equipment_id)
            except Exception:
                pass
        logger.info("BuildSim cleaned up")
    except Exception as e:
        logger.warning(f"Cleanup error: {e}")


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

    # Load detector
    detector = FireDetector()
    try:
        detector.load("models/detector.pkl")
    except FileNotFoundError:
        from fds.models.trainer import train_detector
        detector = train_detector()

    sensors = [
        SmokeSensor(
            config=config, equipment_id=f"smoke-{room}", sensor_id=f"{room}-smoke",
            room=room, level=level, interval=2.0, fire_start=fire_start,
        ),
        COSensor(
            config=config, equipment_id=f"co-{room}", sensor_id=f"{room}-co",
            room=room, level=level, interval=2.0, fire_start=fire_start,
        ),
        TemperatureSensor(
            config=config, equipment_id=f"temp-{room}", sensor_id=f"{room}-temp",
            room=room, level=level, interval=2.0, fire_start=fire_start,
        ),
    ]

    consumer = PipelineConsumer(config)

    async with BuildSimClient(config.buildsim.url) as bs:
        # Load building graph for spread + evacuation
        graph = await bs.get_graph(level=level)

        spread = FireSpreadPredictor(spread_radius=50.0)
        spread.load_graph(graph)

        evac = EvacuationPlanner()
        evac.load_graph(graph)
        # Find exits: edge corridors + staircase rooms
        floor_data = await bs.get_floor(level)
        room_types = {r["name"]: r.get("type", "room") for r in floor_data.get("rooms", [])}

        # Edge corridors: sort corridor nodes by x, take leftmost and rightmost
        corridor_nodes = [
            n for n in graph["nodes"] if room_types.get(n["name"]) == "corridor"
        ]
        corridor_nodes.sort(key=lambda n: n["x"])
        exit_names = []
        if corridor_nodes:
            exit_names.extend([n["name"] for n in corridor_nodes[:2]])
            exit_names.extend([n["name"] for n in corridor_nodes[-2:]])

        # Staircase rooms from cross-floor edges
        try:
            cross_edges = await bs.get_cross_floor_edges()
            for edge in cross_edges:
                if edge.get("from_level", "").endswith(level):
                    stair_room = edge.get("from_name", "")
                    if stair_room and stair_room not in exit_names:
                        exit_names.append(stair_room)
        except Exception as e:
            logger.warning(f"Could not load cross-floor edges: {e}")

        exit_names = list(set(exit_names))
        evac.set_exits(exit_names)
        logger.info(f"Exits: {exit_names}")

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

        # MQTT feed to agent
        mqtt_client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
        def on_message(client, userdata, msg):
            try:
                data = json.loads(msg.payload.decode())
                agent.update_reading(data["room"], data["type"], data["value"])
            except Exception as e:
                logger.warning(f"Agent feed error: {e}")

        mqtt_client.on_message = on_message
        mqtt_client.connect(config.mqtt.host, config.mqtt.port)
        mqtt_client.subscribe(f"{config.mqtt.topic_prefix}/#", qos=1)
        mqtt_client.loop_start()

        print("=" * 60)
        print("Fire Detection System - Full Loop")
        print("=" * 60)
        print(f"Room: {room} ({level})")
        print(f"Fire starts at: t={fire_start}s")
        print(f"Agent model: {config.llm.model}")
        print(f"LLM: {config.llm.base_url}")
        print(f"Spread radius: 50m | Exits: {exit_names[:5]}")
        print(f"BuildSim: {config.buildsim.url}")
        print(f"Press Ctrl+C to stop")
        print("=" * 60)

        tasks = [asyncio.create_task(s.run(bs)) for s in sensors]
        tasks.append(asyncio.create_task(consumer.run()))
        tasks.append(asyncio.create_task(agent.run([(room, level)])))

        try:
            await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            pass
        finally:
            for s in sensors:
                s.stop()
            consumer.stop()
            agent.stop()
            mqtt_client.loop_stop()
            mqtt_client.disconnect()

            await cleanup(bs, sensors)

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
