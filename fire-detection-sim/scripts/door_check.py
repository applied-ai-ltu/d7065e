"""Door states straight from BuildSim, per floor: the ground truth behind the viewer.

python scripts/door_check.py            # all floors
python scripts/door_check.py level2     # one floor, every door listed
"""

import asyncio
import sys
from collections import Counter

from fds.buildsim.client import BuildSimClient
from fds.config import Config


async def main(level_filter):
    cfg = Config.from_yaml("configs/config.yaml")
    async with BuildSimClient(cfg.buildsim.url) as bs:
        rows = []
        for eq in await bs.list_equipment():
            for act in eq.get("actuators") or []:
                aid = str(act.get("id", ""))
                if not aid.endswith("-door"):
                    continue
                level = aid.split("-", 1)[0]
                if level_filter and level != level_filter:
                    continue
                rows.append((level, aid, act.get("state", "?")))
    rows.sort()
    counts = {}
    for level, _aid, state in rows:
        counts.setdefault(level, Counter())[state] += 1
    for level, c in sorted(counts.items()):
        print(f"{level}: " + ", ".join(f"{n} x {s}" for s, n in sorted(c.items())))
    if level_filter:
        for _level, aid, state in rows:
            print(f"  {aid:28s} {state}")


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else None))
