"""Evacuation route planner using BuildSim graph API."""

import heapq
import logging
from dataclasses import dataclass, field

from fds.buildsim.client import BuildSimClient

logger = logging.getLogger(__name__)


@dataclass
class EvacuationRoute:
    from_room: str
    to_exit: str
    path_rooms: list = field(default_factory=list)
    total_weight: float = 0.0
    is_safe: bool = True


class EvacuationPlanner:

    def __init__(self):
        self._nodes = {}
        self._node_by_name = {}
        self._edges = []
        self._exits = []

    def load_graph(self, graph_data):
        for node in graph_data.get("nodes", []):
            self._nodes[node["id"]] = node
            self._node_by_name[node["name"]] = node["id"]
        self._edges = graph_data.get("edges", [])
        logger.info(f"Graph loaded: {len(self._nodes)} nodes, {len(self._edges)} edges")

    def set_exits(self, exit_room_names):
        self._exits = []
        for name in exit_room_names:
            if name in self._node_by_name:
                self._exits.append(self._node_by_name[name])
        logger.info(f"Set {len(self._exits)} exits")

    def _build_adjacency(self, blocked_rooms=None):
        blocked_ids = set()
        if blocked_rooms:
            for room in blocked_rooms:
                if room in self._node_by_name:
                    blocked_ids.add(self._node_by_name[room])

        adj = {}
        for nid in self._nodes:
            if nid not in blocked_ids:
                adj[nid] = []

        for edge in self._edges:
            src, dst = edge["from"], edge["to"]
            weight = edge.get("weight", 1.0)
            if src not in blocked_ids and dst not in blocked_ids:
                if src in adj:
                    adj[src].append((dst, weight))
                if dst in adj:
                    adj[dst].append((src, weight))
        return adj

    def _dijkstra(self, start_id, adj):
        dist = {start_id: 0.0}
        prev = {start_id: None}
        pq = [(0.0, start_id)]

        while pq:
            d, u = heapq.heappop(pq)
            if d > dist.get(u, float("inf")):
                continue
            for v, w in adj.get(u, []):
                nd = d + w
                if nd < dist.get(v, float("inf")):
                    dist[v] = nd
                    prev[v] = u
                    heapq.heappush(pq, (nd, v))
        return dist, prev

    def _reconstruct(self, prev, target):
        path = []
        cur = target
        while cur is not None:
            path.append(cur)
            cur = prev.get(cur)
        path.reverse()
        return path

    def find_routes(self, from_room, blocked_rooms=None):
        if from_room not in self._node_by_name:
            logger.warning(f"Room {from_room} not in graph")
            return []

        # Don't block the start room — people need to leave FROM there
        safe_blocked = [r for r in (blocked_rooms or []) if r != from_room]

        start_id = self._node_by_name[from_room]
        adj = self._build_adjacency(blocked_rooms=safe_blocked)
        dist, prev = self._dijkstra(start_id, adj)

        routes = []
        for exit_id in self._exits:
            exit_name = self._nodes[exit_id]["name"]
            if exit_id in dist and dist[exit_id] < float("inf"):
                path_ids = self._reconstruct(prev, exit_id)
                path_rooms = [self._nodes[nid]["name"] for nid in path_ids]
                routes.append(EvacuationRoute(
                    from_room=from_room,
                    to_exit=exit_name,
                    path_rooms=path_rooms,
                    total_weight=round(dist[exit_id], 1),
                    is_safe=True,
                ))
            else:
                routes.append(EvacuationRoute(
                    from_room=from_room,
                    to_exit=exit_name,
                    path_rooms=[],
                    total_weight=float("inf"),
                    is_safe=False,
                ))

        routes.sort(key=lambda r: r.total_weight)
        return routes

    async def find_routes_via_api(self, bs: BuildSimClient, from_room_id, exit_room_ids, level):
        """Use BuildSim route API for pathfinding."""
        routes = []
        for exit_id in exit_room_ids:
            try:
                result = await bs.get_route(from_room_id, exit_id, level=level)
                path = result.get("path", [])
                path_rooms = [p["name"] for p in path]
                total = sum(
                    p.get("weight", 0) for p in path
                ) if path else float("inf")
                routes.append(EvacuationRoute(
                    from_room=path[0]["name"] if path else "?",
                    to_exit=path[-1]["name"] if path else "?",
                    path_rooms=path_rooms,
                    total_weight=round(total, 1),
                    is_safe=True,
                ))
            except Exception as e:
                logger.warning(f"Route API failed for exit {exit_id}: {e}")
        routes.sort(key=lambda r: r.total_weight)
        return routes
