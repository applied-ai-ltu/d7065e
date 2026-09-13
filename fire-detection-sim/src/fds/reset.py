"""Reset BuildSim to clean state."""

import asyncio
from fds.buildsim.client import BuildSimClient
from fds.config import Config


async def reset():
    config = Config.from_yaml("configs/config.yaml")
    async with BuildSimClient(config.buildsim.url) as bs:
        await bs.set_room_appearance([])
        await bs.set_effects([])
        await bs.set_alerts([])

        equipment = await bs.list_equipment()
        for eq in equipment:
            await bs.delete_equipment(eq["id"])
            print(f"Deleted: {eq['id']}")

        print("BuildSim reset complete")


if __name__ == "__main__":
    asyncio.run(reset())
