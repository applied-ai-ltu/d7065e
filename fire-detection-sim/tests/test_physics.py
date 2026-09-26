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


def test_sprinkler_activates_mechanically_at_68c():
    """Glass bulb breaks at 68C -- no command, no agent, just heat."""
    e = PhysicsEngine()
    e.add_room("A109", "level0")
    e.start_fire("A109", intensity=1.0)

    activated = False
    for _ in range(300):
        e.step(dt=2.0)
        room = e.get_room("A109")
        if room.sprinkler_active:
            activated = True
            assert room.temperature >= 60.0, f"Activated too early at {room.temperature:.1f}C"
            break

    assert activated, "Sprinkler never activated -- fire not hot enough in 600s"


def test_sprinkler_does_not_activate_without_heat():
    """Smoke alone (false alarm) cannot break the bulb."""
    e = PhysicsEngine()
    e.add_room("A109", "level0")
    e.get_room("A109").smoke = 50.0  # heavy smoke, no fire, no heat

    for _ in range(50):
        e.step(dt=2.0)

    assert not e.get_room("A109").sprinkler_active
    assert e.get_room("A109").temperature < 30


def test_sprinkler_stops_temperature_rise():
    """Once active, water suppresses the fire -- temp plateaus or falls."""
    e = PhysicsEngine()
    e.add_room("A109", "level0")
    e.start_fire("A109", intensity=1.0)

    for _ in range(300):
        e.step(dt=2.0)
        if e.get_room("A109").sprinkler_active:
            break
    assert e.get_room("A109").sprinkler_active

    peak = e.get_room("A109").temperature
    for _ in range(60):
        e.step(dt=2.0)
    after = e.get_room("A109").temperature

    assert after <= peak + 3.0, f"Temp kept rising after sprinkler: {peak:.1f} -> {after:.1f}"


def test_stop_fire_resets_sprinkler():
    """After the event, the head is replaced -- ready for the next fire."""
    e = PhysicsEngine()
    e.add_room("A109", "level0")
    e.set_sprinkler("A109", True)
    e.start_fire("A109")
    e.stop_fire("A109")
    assert not e.get_room("A109").sprinkler_active
