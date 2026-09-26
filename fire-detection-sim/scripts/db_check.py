"""Row counts for the edge/server tables — quick verification after a run."""

import asyncio

import asyncpg

from fds.config import Config
from fds.secrets import get_secrets

QUERIES = {
    "edge_decisions (last 15 min)": """
        SELECT kind, decision, severity, COUNT(*) AS n
        FROM fire_detection_sim.edge_decisions
        WHERE time > NOW() - INTERVAL '15 min'
        GROUP BY 1, 2, 3 ORDER BY 1, 2, 3""",
    "heartbeats (last 15 min)": """
        SELECT level, role, node, COUNT(*) AS n, MAX(time) AS last_seen
        FROM fire_detection_sim.heartbeats
        WHERE time > NOW() - INTERVAL '15 min'
        GROUP BY 1, 2, 3 ORDER BY 1, 2""",
    "llm_overrides (last 15 min)": """
        SELECT override_type, COUNT(*) AS n
        FROM fire_detection_sim.llm_overrides
        WHERE time > NOW() - INTERVAL '15 min'
        GROUP BY 1 ORDER BY 1""",
}


async def main():
    cfg = Config.from_yaml("configs/config.yaml")
    cfg.database.password = get_secrets().get("db_password", "")
    conn = await asyncpg.connect(
        host=cfg.database.host,
        port=cfg.database.port,
        user=cfg.database.user,
        password=cfg.database.password,
        database=cfg.database.database,
    )
    try:
        for title, q in QUERIES.items():
            print(f"\n== {title}")
            for r in await conn.fetch(q):
                print("  ", dict(r))
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
