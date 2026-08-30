from __future__ import annotations

import pytest
from conftest import GUILD, STAFF
from kimi_agent_module_api.testing import MemoryStorage

from kimi_agent_custom_commands.models import starter_document
from kimi_agent_custom_commands.store import CommandStore, ProposalCandidate, RevisionConflict

pytestmark = pytest.mark.asyncio


async def test_crud_uses_stable_id_and_revision(storage: MemoryStorage) -> None:
    store = CommandStore(storage)
    first = await store.create(
        GUILD, "hello", "Says hello", starter_document(), actor_id=STAFF, now=1
    )
    updated = await store.update(
        first.command_id,
        GUILD,
        "hi",
        "Says hi",
        first.document,
        expected_revision=1,
        actor_id=STAFF,
        now=2,
    )

    assert updated.command_id == first.command_id
    assert updated.revision == 2
    assert await store.get(GUILD, "hello") is None
    with pytest.raises(RevisionConflict):
        await store.update(
            first.command_id,
            GUILD,
            "late",
            "Late",
            first.document,
            expected_revision=1,
            actor_id=STAFF,
            now=3,
        )


async def test_approval_is_atomic_and_rejects_stale_target(storage: MemoryStorage) -> None:
    store = CommandStore(storage)
    command = await store.create(
        GUILD, "hello", "Says hello", starter_document(), actor_id=STAFF, now=1
    )
    proposal = await store.create_proposal(
        proposal_id="p1",
        guild_id=GUILD,
        command=command,
        command_name="hello",
        operation="replace",
        candidate=ProposalCandidate("hello", "Updated", starter_document()),
        proposer_id=STAFF,
        summary="Update",
        now=2,
    )
    await store.update(
        command.command_id,
        GUILD,
        "hello",
        "Concurrent",
        command.document,
        expected_revision=1,
        actor_id=999,
        now=3,
    )

    result = await store.approve_proposal(
        proposal.proposal_id, guild_id=GUILD, decider_id=STAFF, now=4
    )

    assert not result.applied
    assert "changed" in result.reason
    assert (await store.get_proposal("p1")).state == "rejected"  # type: ignore[union-attr]
    assert (await store.get(GUILD, "hello")).description == "Concurrent"  # type: ignore[union-attr]


async def test_replace_rename_collision_is_rejected_in_approval_transaction(
    storage: MemoryStorage,
) -> None:
    store = CommandStore(storage)
    source = await store.create(
        GUILD, "source", "Source", starter_document(), actor_id=STAFF, now=1
    )
    await store.create(GUILD, "taken", "Taken", starter_document(), actor_id=STAFF, now=1)
    await store.create_proposal(
        proposal_id="p2",
        guild_id=GUILD,
        command=source,
        command_name="source",
        operation="replace",
        candidate=ProposalCandidate("taken", "Rename", starter_document()),
        proposer_id=STAFF,
        summary="Rename",
        now=2,
    )

    result = await store.approve_proposal("p2", guild_id=GUILD, decider_id=STAFF, now=3)

    assert not result.applied
    assert "owned" in result.reason
    assert (await store.get_proposal("p2")).state == "rejected"  # type: ignore[union-attr]
    assert await store.get(GUILD, "source") is not None
    assert await store.get(GUILD, "taken") is not None


async def test_successful_approval_mutates_command_and_proposal_together(
    storage: MemoryStorage,
) -> None:
    store = CommandStore(storage)
    command = await store.create(GUILD, "hello", "Old", starter_document(), actor_id=STAFF, now=1)
    await store.create_proposal(
        proposal_id="p3",
        guild_id=GUILD,
        command=command,
        command_name="hello",
        operation="replace",
        candidate=ProposalCandidate("hello", "New", starter_document()),
        proposer_id=STAFF,
        summary="Update",
        now=2,
    )
    result = await store.approve_proposal("p3", guild_id=GUILD, decider_id=STAFF, now=3)

    assert result.applied
    assert (await store.get(GUILD, "hello")).description == "New"  # type: ignore[union-attr]
    assert (await store.get_proposal("p3")).state == "applied"  # type: ignore[union-attr]


async def test_deterministic_approval_compensation_restores_prior_row(
    storage: MemoryStorage,
) -> None:
    store = CommandStore(storage)
    command = await store.create(GUILD, "hello", "Old", starter_document(), actor_id=STAFF, now=1)
    await store.create_proposal(
        proposal_id="p4",
        guild_id=GUILD,
        command=command,
        command_name="hello",
        operation="replace",
        candidate=ProposalCandidate("renamed", "New", starter_document()),
        proposer_id=STAFF,
        summary="Rename",
        now=2,
    )
    result = await store.approve_proposal("p4", guild_id=GUILD, decider_id=STAFF, now=3)
    await store.compensate_approval(result, decider_id=STAFF, reason="Discord collision", now=4)

    restored = await store.get(GUILD, "hello")
    assert restored is not None and restored.revision == command.revision
    assert await store.get(GUILD, "renamed") is None
    assert (await store.get_proposal("p4")).state == "rejected"  # type: ignore[union-attr]
