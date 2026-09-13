"""Test evacuation planner."""

from fds.models.evacuation import EvacuationPlanner


def make_graph():
    return {
        "nodes": [
            {"id": 1, "name": "A109", "x": 0, "y": 0},
            {"id": 2, "name": "Corridor", "x": 10, "y": 0},
            {"id": 3, "name": "Exit_A", "x": 20, "y": 0},
            {"id": 4, "name": "A110", "x": 0, "y": 10},
            {"id": 5, "name": "Exit_B", "x": 0, "y": 20},
        ],
        "edges": [
            {"from": 1, "to": 2, "weight": 10.0},
            {"from": 2, "to": 3, "weight": 10.0},
            {"from": 1, "to": 4, "weight": 10.0},
            {"from": 4, "to": 5, "weight": 10.0},
        ],
    }


def test_finds_routes_to_exits():
    planner = EvacuationPlanner()
    planner.load_graph(make_graph())
    planner.set_exits(["Exit_A", "Exit_B"])

    routes = planner.find_routes("A109")
    assert len(routes) == 2
    assert all(r.is_safe for r in routes)


def test_avoids_blocked_rooms():
    planner = EvacuationPlanner()
    planner.load_graph(make_graph())
    planner.set_exits(["Exit_A", "Exit_B"])

    routes = planner.find_routes("A109", blocked_rooms=["Corridor"])
    exit_a = [r for r in routes if r.to_exit == "Exit_A"][0]
    assert not exit_a.is_safe or len(exit_a.path_rooms) == 0


def test_shortest_route_first():
    planner = EvacuationPlanner()
    planner.load_graph(make_graph())
    planner.set_exits(["Exit_A", "Exit_B"])

    routes = planner.find_routes("A109")
    assert routes[0].total_weight <= routes[1].total_weight
