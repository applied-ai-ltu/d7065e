"""Verify the BuildSim client can connect."""

import os
skipci = pytest.mark.skipif(os.getenv("CI") == "true", reason="No BuildSim in CI")

import pytest
from fds.buildsim.client import BuildSimClient
from fds.config import Config


@pytest.fixture
def config():
    return Config.from_yaml("configs/config.yaml")


@pytest.mark.asyncio
@skipci
async def test_health(config):
    async with BuildSimClient(config.buildsim.url) as client:
        result = await client.health()
        assert result["status"] == "ok"


@pytest.mark.asyncio
@skipci
async def test_get_building(config):
    async with BuildSimClient(config.buildsim.url) as client:
        building = await client.get_building()
        assert building["name"] == "A-Building (LTU)"
        assert len(building["levels"]) == 3


@pytest.mark.asyncio
@skipci
async def test_get_floor(config):
    async with BuildSimClient(config.buildsim.url) as client:
        floor = await client.get_floor("level0")
        assert floor is not None
