from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path

import pytest_asyncio
from kimi_agent_module_api import ModuleCapabilities, ModuleRuntimeContext
from kimi_agent_module_api.testing import (
    FakeDiscordActions,
    FakeEvents,
    FakeHealth,
    FakeHttp,
    FakeInteractions,
    FakeScheduler,
    FakeServiceRegistry,
    FakeTrust,
    MemoryStorage,
    load_context,
)

from kimi_agent_custom_commands.migrations import MIGRATIONS
from kimi_agent_custom_commands.module import MODULE_NAME, CustomCommandsModule
from kimi_agent_custom_commands.spec import SPEC

GUILD = 100
CHANNEL = 200
STAFF = 300


@dataclass(slots=True)
class Harness:
    module: CustomCommandsModule
    ctx: ModuleRuntimeContext
    storage: MemoryStorage
    interactions: FakeInteractions
    discord: FakeDiscordActions
    health: FakeHealth


@pytest_asyncio.fixture
async def storage() -> AsyncIterator[MemoryStorage]:
    async with MemoryStorage.open(MODULE_NAME) as memory:
        await memory.migrate(MIGRATIONS)
        yield memory


@pytest_asyncio.fixture
async def started(storage: MemoryStorage, tmp_path: Path) -> AsyncIterator[Harness]:
    load, _recorder = load_context(None)
    module = SPEC.create(load)
    assert isinstance(module, CustomCommandsModule)
    interactions = FakeInteractions(MODULE_NAME)
    discord = FakeDiscordActions(MODULE_NAME, SPEC.permissions.discord_actions)
    health = FakeHealth()
    ctx = ModuleRuntimeContext(
        module_name=MODULE_NAME,
        is_guild_active=lambda _guild_id: True,
        current_config_dir=lambda: tmp_path,
        capabilities=ModuleCapabilities(
            frozenset(
                {
                    "discord.guild_commands.v1",
                    "discord.modals.v1",
                    "discord.components_v2.v1",
                }
            ),
            members_intent=False,
            message_content_intent=False,
        ),
        events=FakeEvents(MODULE_NAME),
        scheduler=FakeScheduler(),
        storage=storage,
        health=health,
        discord=discord,
        interactions=interactions,
        http=FakeHttp(),
        services=FakeServiceRegistry(),
        trust=FakeTrust(default="staff"),
    )
    await module.start(ctx)
    try:
        yield Harness(module, ctx, storage, interactions, discord, health)
    finally:
        await module.close()
