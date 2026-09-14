"""Control API — trigger fire scenarios from Grafana.

FastAPI service that manages the simulation state.
Grafana calls these endpoints via Business Forms plugin or simple links.
"""

import logging
import time

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from fds.physics.engine import PhysicsEngine

logger = logging.getLogger(__name__)

_engine = None
_start_time = 0


def set_engine(engine):
    global _engine, _start_time
    _engine = engine
    _start_time = time.time()


app = FastAPI(title="Fire Detection Control API", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


class FireRequest(BaseModel):
    room: str = "A109"
    level: str = "level0"
    intensity: float = 1.0


@app.get("/status")
async def get_status():
    if not _engine:
        raise HTTPException(status_code=503, detail="Engine not initialized")
    active = [f.room for f in _engine.fires]
    return {
        "running": True,
        "elapsed": round(time.time() - _start_time, 1),
        "rooms": len(_engine.rooms),
        "active_fires": active,
    }


@app.post("/fire/start")
async def start_fire(req: FireRequest):
    if not _engine:
        raise HTTPException(status_code=503, detail="Engine not initialized")
    if req.room not in _engine.rooms:
        raise HTTPException(status_code=404, detail=f"Room {req.room} not found")
    if any(f.room == req.room for f in _engine.fires):
        return {"message": f"Fire already active in {req.room}"}
    _engine.start_fire(req.room, intensity=req.intensity)
    return {"message": f"Fire started in {req.room}", "intensity": req.intensity}


@app.post("/fire/stop")
async def stop_fire(req: FireRequest):
    if not _engine:
        raise HTTPException(status_code=503, detail="Engine not initialized")
    _engine.stop_fire(req.room)
    return {"message": f"Fire stopped in {req.room}"}


@app.post("/false-alarm/trigger")
async def trigger_false_alarm(req: FireRequest):
    if not _engine:
        raise HTTPException(status_code=503, detail="Engine not initialized")
    room = _engine.get_room(req.room)
    if not room:
        raise HTTPException(status_code=404, detail=f"Room {req.room} not found")
    room.smoke += 15.0
    return {"message": f"False alarm triggered in {req.room}"}


@app.post("/system/reset")
async def reset_system():
    if not _engine:
        raise HTTPException(status_code=503, detail="Engine not initialized")
    for room in _engine.rooms.values():
        room.smoke = 0.0
        room.co = 0.0
        room.temperature = 21.0
        room.on_fire = False
        room.fire_intensity = 0.0
    _engine.fires.clear()
    return {"message": "System reset"}


@app.get("/rooms")
async def list_rooms():
    if not _engine:
        raise HTTPException(status_code=503, detail="Engine not initialized")
    rooms = []
    for name, room in _engine.rooms.items():
        rooms.append({
            "name": name,
            "level": room.level,
            "temperature": round(room.temperature, 1),
            "smoke": round(room.smoke, 1),
            "co": round(room.co, 1),
            "on_fire": room.on_fire,
            "occupancy": room.occupancy,
            "sprinkler_active": room.sprinkler_active,
        })
    return rooms
