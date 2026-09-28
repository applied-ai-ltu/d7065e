"""Standalone Control API — manages the entire simulation from Grafana.

Runs independently. Starts/stops the simulation, triggers fires.
"""

import asyncio
import json
import logging
import math
import time

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from fds.agent.agent import FireAgent
from fds.buildsim.client import BuildSimClient
from fds.config import Config
from fds.models.detector import FireDetector
from fds.models.evacuation import EvacuationPlanner
from fds.models.spread import FireSpreadPredictor
from fds.physics.actuator_process import ActuatorConfig, ActuatorProcess
from fds.physics.engine import PhysicsEngine
from fds.physics.occupancy import OccupancySimulator
from fds.physics.sensor_gateway import SensorConfig, SensorGateway
from fds.pipeline.consumer import PipelineConsumer
from fds.pipeline.schema import init_database
from fds.secrets import get_secrets

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

app = FastAPI(title="Fire Detection Control API", version="0.2.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


class SimState:
    """Holds all simulation state."""

    def __init__(self):
        self.running = False
        self.engine = None
        self.tasks = []
        self.components = []
        self.loop = None
        self.thread = None
        self.config = None
        self.secrets = None
        self.start_time = 0
        self.agent = None
        self.bs = None


sim = SimState()


class FireRequest(BaseModel):
    room: str = "A109"
    intensity: float = 1.0


@app.get("/status")
async def get_status():
    active = [f.room for f in sim.engine.fires] if sim.engine else []
    return {
        "running": sim.running,
        "elapsed": round(time.time() - sim.start_time, 1) if sim.running else 0,
        "rooms": len(sim.engine.rooms) if sim.engine else 0,
        "active_fires": active,
    }


@app.post("/simulation/start")
async def start_simulation():
    if sim.running:
        return {"message": "Simulation already running"}

    sim.config = Config.from_yaml("configs/config.yaml")
    sim.secrets = get_secrets()
    sim.config.database.password = sim.secrets.get("db_password", "")

    # Init DB
    await init_database(
        sim.config.database.host,
        sim.config.database.port,
        sim.config.database.user,
        sim.config.database.password,
        sim.config.database.database,
    )

    # Build everything
    bs = BuildSimClient(sim.config.buildsim.url)
    await bs.__aenter__()

    graph = await bs.get_graph(level="level0")
    floor_data = await bs.get_floor("level0")
    room_types = {r["name"]: r.get("type", "room") for r in floor_data.get("rooms", [])}

    # Physics engine
    engine = PhysicsEngine(outdoor_temp=-5.0)
    node_positions = {}
    for node in graph["nodes"]:
        engine.add_room(node["name"], "level0")
        node_positions[node["name"]] = (node["x"], node["y"])

    for name, pos in node_positions.items():
        neighbors = []
        for other_name, other_pos in node_positions.items():
            if other_name == name:
                continue
            dist = math.sqrt((pos[0] - other_pos[0]) ** 2 + (pos[1] - other_pos[1]) ** 2)
            if dist < 50:
                neighbors.append((other_name, dist))
        engine.set_neighbors(name, neighbors)

    sim.engine = engine
    logger.info(f"Physics engine: {len(engine.rooms)} rooms")

    # Occupancy
    occupancy_sim = OccupancySimulator(room_types=room_types)

    # Sensor gateway
    gateway = SensorGateway(sim.config, engine, interval=2.0)
    room = "A109"
    eq_id = f"sensors-{room}"
    for stype, unit, noise, lag, drift, stuck in [
        ("smoke", "%obs/m", 0.3, 2.0, 0.001, 0.005),
        ("co", "ppm", 0.5, 3.0, 0.002, 0.005),
        ("temperature", "C", 0.15, 1.0, 0.0005, 0.002),
    ]:
        gateway.add_sensor(
            SensorConfig(
                sensor_id=f"{room}-{stype}",
                equipment_id=eq_id,
                room=room,
                level="level0",
                sensor_type=stype,
                unit=unit,
                noise_sigma=noise,
                lag_seconds=lag,
                drift_rate=drift,
                stuck_probability=stuck,
            )
        )

    # Actuators
    sprinkler = ActuatorProcess(
        ActuatorConfig(
            actuator_id=f"{room}-sprinkler",
            equipment_id=f"sprinkler-{room}",
            room=room,
            level="level0",
            actuator_type="sprinkler",
            travel_rate=0.5,
            stuck_probability=0.005,
        )
    )
    door = ActuatorProcess(
        ActuatorConfig(
            actuator_id=f"{room}-door",
            equipment_id=f"door-{room}",
            room=room,
            level="level0",
            actuator_type="fire_door",
            travel_rate=0.3,
            stuck_probability=0.002,
        )
    )

    # Pipeline consumer
    consumer = PipelineConsumer(sim.config)

    # Detector
    detector = FireDetector()
    try:
        detector.load("models/detector.pkl")
    except FileNotFoundError:
        from fds.models.trainer import train_detector

        detector = train_detector()

    # Spread + evac
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
            if edge.get("from_level", "").endswith("level0"):
                stair = edge.get("from_name", "")
                if stair and stair not in exit_names:
                    exit_names.append(stair)
    except Exception:
        pass
    evac.set_exits(list(set(exit_names)))

    # Agent
    agent = FireAgent(
        bs=bs,
        detector=detector,
        spread_predictor=spread,
        evacuation_planner=evac,
        llm_url=sim.config.llm.base_url,
        llm_api_key=sim.secrets.get("litellm_api_key", ""),
        model=sim.config.llm.model,
        tick_interval=5.0,
        db_config={
            "host": sim.config.database.host,
            "port": sim.config.database.port,
            "user": sim.config.database.user,
            "password": sim.config.database.password,
            "database": sim.config.database.database,
        },
    )

    # Physics loop (no auto-fire)
    async def physics_loop():
        sim_hour = 9.0
        while sim.running:
            try:
                await engine.sync_actuators_from_buildsim(bs)
            except Exception:
                pass
            sim_hour += 2.0 / 60.0
            if sim_hour >= 24:
                sim_hour = 7.0
            for room_name in engine.rooms:
                occ = occupancy_sim.get_occupancy(room_name, sim_hour=sim_hour)
                engine.set_occupancy(room_name, occ)
            engine.step(dt=2.0)
            await asyncio.sleep(2.0)

    # MQTT feed to agent
    import paho.mqtt.client as mqtt

    mqtt_client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)

    def on_message(client, userdata, msg):
        try:
            data = json.loads(msg.payload.decode())
            agent.update_reading(data["room"], data["type"], data["value"])
        except Exception:
            pass

    mqtt_client.on_message = on_message
    mqtt_client.connect(sim.config.mqtt.host, sim.config.mqtt.port)
    mqtt_client.subscribe(f"{sim.config.mqtt.topic_prefix}/#", qos=1)
    mqtt_client.loop_start()

    sim.running = True
    sim.start_time = time.time()
    sim.components = [gateway, sprinkler, door, consumer, agent, mqtt_client, bs]

    # Start all tasks
    sim.tasks = [
        asyncio.create_task(physics_loop()),
        asyncio.create_task(gateway.run(bs)),
        asyncio.create_task(sprinkler.run(bs)),
        asyncio.create_task(door.run(bs)),
        asyncio.create_task(consumer.run()),
        asyncio.create_task(agent.run([("A109", "level0")])),
    ]

    logger.info("SIMULATION STARTED")
    return {"message": "Simulation started", "rooms": len(engine.rooms)}


@app.post("/simulation/stop")
async def stop_simulation():
    if not sim.running:
        return {"message": "Simulation not running"}

    sim.running = False

    # Stop components
    for comp in sim.components:
        if hasattr(comp, "stop"):
            comp.stop()
        elif hasattr(comp, "loop_stop"):
            comp.loop_stop()
            comp.disconnect()

    # Cancel tasks
    for task in sim.tasks:
        task.cancel()

    # Cleanup BuildSim
    bs = sim.components[-1] if sim.components else None
    if bs:
        try:
            await bs.set_room_appearance([])
            await bs.set_effects([])
            await bs.set_alerts([])
            await bs.set_coverage([])
            equipment = await bs.list_equipment()
            for eq in equipment:
                try:
                    await bs.delete_equipment(eq["id"])
                except Exception:
                    pass
        except Exception:
            pass
        try:
            await bs.__aexit__(None, None, None)
        except Exception:
            pass

    sim.tasks = []
    sim.components = []
    sim.engine = None

    logger.info("SIMULATION STOPPED")
    return {"message": "Simulation stopped"}


@app.post("/fire/start")
async def start_fire(req: FireRequest):
    if not sim.running or not sim.engine:
        raise HTTPException(
            status_code=503, detail="Simulation not running. Start simulation first."
        )
    if req.room not in sim.engine.rooms:
        raise HTTPException(status_code=404, detail=f"Room {req.room} not found")
    if any(f.room == req.room for f in sim.engine.fires):
        return {"message": f"Fire already active in {req.room}"}
    sim.engine.start_fire(req.room, intensity=req.intensity)
    return {"message": f"Fire started in {req.room}", "intensity": req.intensity}


@app.post("/fire/stop")
async def stop_fire(req: FireRequest):
    if not sim.running or not sim.engine:
        raise HTTPException(status_code=503, detail="Simulation not running")
    sim.engine.stop_fire(req.room)
    return {"message": f"Fire stopped in {req.room}"}


@app.post("/false-alarm/trigger")
async def trigger_false_alarm(req: FireRequest):
    if not sim.running or not sim.engine:
        raise HTTPException(status_code=503, detail="Simulation not running")
    room = sim.engine.get_room(req.room)
    if not room:
        raise HTTPException(status_code=404, detail=f"Room {req.room} not found")
    room.smoke += 15.0
    return {"message": f"False alarm triggered in {req.room}"}


@app.post("/viz/clear")
async def clear_visualisation():
    """Reset BuildSim effects, tints, alerts and coverage (demo prep / stale state)."""
    if sim.agent is not None:
        await sim.agent._clear_all_viz()
        return {"message": "visualisation cleared (agent state reset)"}
    if sim.bs is None:
        raise HTTPException(status_code=503, detail="Simulation not running")
    bs = sim.bs
    for call in (bs.set_room_appearance, bs.set_effects, bs.set_alerts, bs.set_coverage):
        await call([])
    return {"message": "visualisation cleared"}


if __name__ == "__main__":
    import uvicorn

    # Init tracing
    from fds.tracing import setup_phoenix

    setup_phoenix("d7065e")

    print("=" * 60)
    print("Fire Detection Control API")
    print("=" * 60)
    print("Endpoints:")
    print("  POST /simulation/start  - Start the simulation")
    print("  POST /simulation/stop   - Stop the simulation")
    print("  POST /fire/start        - Trigger fire")
    print("  POST /fire/stop         - Stop fire")
    print("  POST /false-alarm/trigger - Trigger false alarm")
    print("  GET  /status            - System status")
    print("  GET  /docs              - Swagger UI")
    print("=" * 60)
    uvicorn.run(app, host="0.0.0.0", port=8089)
