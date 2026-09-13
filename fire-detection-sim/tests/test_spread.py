"""Test fire spread predictor."""

from fds.models.spread import FireSpreadPredictor


def make_graph():
    return {
        "nodes": [
            {"id": 97, "name": "A109", "x": 264.3, "y": 249.4},
            {"id": 96, "name": "A108", "x": 258.0, "y": 273.0},
            {"id": 98, "name": "A110", "x": 270.0, "y": 222.0},
            {"id": 91, "name": "A103", "x": 280.5, "y": 276.9},
            {"id": 95, "name": "A107", "x": 250.0, "y": 297.0},
            {"id": 50, "name": "FarRoom", "x": 100.0, "y": 100.0},
        ],
        "edges": [],
    }


def test_predicts_nearby_rooms():
    pred = FireSpreadPredictor(spread_radius=50.0)
    pred.load_graph(make_graph())

    risks = pred.predict_spread("A109", "level0")
    room_names = [r.room for r in risks]
    assert "A108" in room_names
    assert "A110" in room_names
    assert "FarRoom" not in room_names


def test_risk_decreases_with_distance():
    pred = FireSpreadPredictor(spread_radius=50.0)
    pred.load_graph(make_graph())

    risks = pred.predict_spread("A109", "level0")
    risk_map = {r.room: r.risk_score for r in risks}
    assert risk_map.get("A108", 0) > risk_map.get("A107", 0)


def test_risk_increases_with_temperature():
    pred = FireSpreadPredictor(spread_radius=50.0)
    pred.load_graph(make_graph())

    risks_cold = pred.predict_spread("A109", "level0", temperature=21)
    risks_hot = pred.predict_spread("A109", "level0", temperature=50)

    score_cold = sum(r.risk_score for r in risks_cold)
    score_hot = sum(r.risk_score for r in risks_hot)
    assert score_hot > score_cold


def test_unknown_room_returns_empty():
    pred = FireSpreadPredictor()
    pred.load_graph(make_graph())
    assert pred.predict_spread("Z999", "level0") == []
