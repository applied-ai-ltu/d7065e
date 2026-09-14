"""Test occupancy simulator."""

from fds.physics.occupancy import OccupancySimulator


def test_empty_at_night():
    sim = OccupancySimulator(room_types={"A109": "room"})
    assert sim.get_occupancy("A109", sim_hour=3.0) == 0


def test_occupied_during_morning():
    sim = OccupancySimulator(room_types={"A109": "room"})
    occ = sim.get_occupancy("A109", sim_hour=10.0)
    assert occ > 5, f"Should be occupied at 10am: {occ}"


def test_lunch_dip():
    sim = OccupancySimulator(room_types={"A109": "room"})
    morning = sim.get_occupancy("A109", sim_hour=10.0)
    lunch = sim.get_occupancy("A109", sim_hour=12.0)
    assert lunch < morning, f"Lunch should be less than morning: {lunch} >= {morning}"


def test_corridors_less_occupied():
    sim = OccupancySimulator(room_types={"A109": "room", "A103": "corridor"})
    room_occ = sim.get_occupancy("A109", sim_hour=10.0)
    corr_occ = sim.get_occupancy("A103", sim_hour=10.0)
    assert corr_occ < room_occ, f"Corridor more occupied than room: {corr_occ} >= {room_occ}"


def test_no_two_days_identical():
    sim = OccupancySimulator(room_types={"A109": "room"})
    day1 = [sim.get_occupancy("A109", sim_hour=h) for h in range(7, 18)]
    day2 = [sim.get_occupancy("A109", sim_hour=h) for h in range(7, 18)]
    assert day1 != day2, "Two runs should not be identical"
