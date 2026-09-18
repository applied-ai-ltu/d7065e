"""Test the physics engine."""

from fds.physics.engine import PhysicsEngine


def test_room_stable_without_fire():
    engine = PhysicsEngine()
    engine.add_room("A109", "level0", initial_temp=21.0)
    for _ in range(50):
        engine.step(dt=2.0)
    room = engine.get_room("A109")
    assert 15 < room.temperature < 25
    assert room.smoke < 0.1
    assert room.co < 0.1


def test_fire_raises_all():
    engine = PhysicsEngine()
    engine.add_room("A109", "level0")
    engine.start_fire("A109")
    for _ in range(30):
        engine.step(dt=2.0)
    room = engine.get_room("A109")
    assert room.temperature > 25
    assert room.smoke > 5
    assert room.co > 5


def test_sprinkler_reduces_fire():
    e1 = PhysicsEngine()
    e1.add_room("A109", "level0")
    e1.start_fire("A109")

    e2 = PhysicsEngine()
    e2.add_room("A109", "level0")
    e2.start_fire("A109")
    e2.set_sprinkler("A109", True)

    for _ in range(30):
        e1.step(dt=2.0)
        e2.step(dt=2.0)

    assert e2.get_room("A109").temperature < e1.get_room("A109").temperature
    assert e2.get_room("A109").smoke < e1.get_room("A109").smoke


def test_fire_propagates_to_neighbor():
    # With fire next door
    e1 = PhysicsEngine()
    e1.add_room("A109", "level0")
    e1.add_room("A108", "level0")
    e1.set_neighbors("A109", [("A108", 25.0)])
    e1.start_fire("A109")

    # Without fire (control)
    e2 = PhysicsEngine()
    e2.add_room("A109", "level0")
    e2.add_room("A108", "level0")
    e2.set_neighbors("A109", [("A108", 25.0)])

    for _ in range(40):
        e1.step(dt=2.0)
        e2.step(dt=2.0)

    # Neighbor should be warmer with fire than without
    assert e1.get_room("A108").temperature > e2.get_room("A108").temperature
    # Smoke and CO should propagate
    assert e1.get_room("A108").smoke > 0
    assert e1.get_room("A108").co > 0


def test_stop_fire():
    engine = PhysicsEngine()
    engine.add_room("A109", "level0")
    engine.start_fire("A109")
    for _ in range(20):
        engine.step(dt=2.0)
    engine.stop_fire("A109")
    room = engine.get_room("A109")
    assert not room.on_fire


def test_occupancy_adds_heat():
    e1 = PhysicsEngine()
    e1.add_room("A109", "level0")

    e2 = PhysicsEngine()
    e2.add_room("A109", "level0")
    e2.set_occupancy("A109", 20)

    for _ in range(50):
        e1.step(dt=2.0)
        e2.step(dt=2.0)

    assert e2.get_room("A109").temperature > e1.get_room("A109").temperature
