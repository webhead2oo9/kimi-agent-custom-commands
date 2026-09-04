from __future__ import annotations

from functools import partial

import pytest
from conftest import CHANNEL, GUILD, STAFF, Harness
from kimi_agent_module_api import ModuleToolContext
from kimi_agent_module_api.contracts import ButtonSpec, LayoutText
from kimi_agent_module_api.testing import FakeInteraction as _FakeInteraction
from kimi_agent_module_api.testing import load_context
from kimi_agent_module_api.trust import TrustTier

from kimi_agent_custom_commands.editor import SessionStore, block_modal, editor_components
from kimi_agent_custom_commands.models import (
    CommandDocument,
    Page,
    TextBlock,
    document_to_dict,
    starter_document,
)
from kimi_agent_custom_commands.spec import SPEC
from kimi_agent_custom_commands.store import CommandStore

FakeInteraction = partial(_FakeInteraction, module_name=SPEC.name)

pytestmark = pytest.mark.asyncio


def _custom_id(started: Harness, key: str, *parts: str) -> str:
    return started.interactions.custom_id(key, *parts)


async def test_create_editor_save_and_member_invocation(started: Harness) -> None:
    create = started.interactions.commands["custom-command.create"][1]
    opening = FakeInteraction(
        guild_id=GUILD,
        channel_id=CHANNEL,
        user_id=STAFF,
        options={"name": "hello", "description": "Says hello"},
    )
    await create(opening)
    token = opening.last.components[0].parts[0]
    save = started.interactions.components[("button", "editor_save")]
    saved = FakeInteraction(
        guild_id=GUILD,
        channel_id=CHANNEL,
        user_id=STAFF,
        custom_id=_custom_id(started, "editor_save", token),
        message_uses_layout=True,
    )
    await save(saved)

    assert saved.deferred is True
    assert saved.last.layout is not None
    assert saved.last.layout.items == (LayoutText("Saved `/hello`."),)
    assert "hello" in started.interactions.guild_commands[GUILD]
    dynamic = started.interactions.guild_commands[GUILD]["hello"][1]
    invoked = FakeInteraction(guild_id=GUILD, options={"hidden": True})
    await dynamic(invoked)
    assert invoked.last.layout is not None
    assert invoked.last.ephemeral is True


async def test_failed_modal_edit_does_not_mutate_draft(started: Harness) -> None:
    create = started.interactions.commands["custom-command.create"][1]
    opening = FakeInteraction(
        guild_id=GUILD, user_id=STAFF, options={"name": "hello", "description": "Says hello"}
    )
    await create(opening)
    token = opening.last.components[0].parts[0]
    session = started.module._sessions.get(
        token, guild_id=GUILD, user_id=STAFF, now=started.module._clock()
    )
    assert session is not None
    before = (session.name, session.description, session.document, session.dirty)
    submit = FakeInteraction(
        guild_id=GUILD,
        user_id=STAFF,
        custom_id=_custom_id(started, "editor_modal", token, "general"),
        text_values={"name": "NOT VALID", "description": "Changed"},
    )
    await started.interactions.components[("modal", "editor_modal")](submit)

    assert "invalid" in (submit.last.content or "")
    assert (session.name, session.description, session.document, session.dirty) == before


async def test_stale_block_modal_fingerprint_is_rejected(started: Harness) -> None:
    create = started.interactions.commands["custom-command.create"][1]
    opening = FakeInteraction(
        guild_id=GUILD,
        user_id=STAFF,
        options={"name": "hello", "description": "Says hello"},
    )
    await create(opening)
    token = opening.last.components[0].parts[0]
    session = started.module._sessions.get(
        token, guild_id=GUILD, user_id=STAFF, now=started.module._clock()
    )
    assert session is not None
    modal = block_modal(session, "text", editing=True)
    assert modal is not None
    session.replace_block(TextBlock(text="Changed elsewhere"))
    submit = FakeInteraction(
        guild_id=GUILD,
        user_id=STAFF,
        custom_id=_custom_id(started, modal.key, *modal.parts),
        text_values={"text": "Stale overwrite"},
    )
    await started.interactions.components[("modal", "editor_modal")](submit)
    assert "selected block changed" in (submit.last.content or "")
    assert session.page.blocks[0] == TextBlock(text="Changed elsewhere")


async def test_stale_block_delete_fingerprint_is_rejected(started: Harness) -> None:
    create = started.interactions.commands["custom-command.create"][1]
    opening = FakeInteraction(
        guild_id=GUILD,
        user_id=STAFF,
        options={"name": "hello", "description": "Says hello"},
    )
    await create(opening)
    token = opening.last.components[0].parts[0]
    session = started.module._sessions.get(
        token, guild_id=GUILD, user_id=STAFF, now=started.module._clock()
    )
    assert session is not None
    session.document = CommandDocument(
        pages=(
            Page(
                "main",
                "Main",
                blocks=(TextBlock(text="One"), TextBlock(text="Two"), TextBlock(text="Three")),
            ),
        )
    )
    session.selected_block = 0
    delete = next(
        component
        for component in editor_components(session)
        if isinstance(component, ButtonSpec) and component.key == "editor_block_delete"
    )
    custom_id = _custom_id(started, delete.key, *delete.parts)
    handler = started.interactions.components[("button", "editor_block_delete")]

    first = FakeInteraction(
        guild_id=GUILD,
        user_id=STAFF,
        custom_id=custom_id,
        message_uses_layout=True,
    )
    await handler(first)
    assert session.page.blocks == (TextBlock(text="Two"), TextBlock(text="Three"))

    stale = FakeInteraction(
        guild_id=GUILD,
        user_id=STAFF,
        custom_id=custom_id,
        message_uses_layout=True,
    )
    await handler(stale)
    assert "selected block changed" in (stale.last.content or "")
    assert session.page.blocks == (TextBlock(text="Two"), TextBlock(text="Three"))


async def test_close_verifies_editor_owner(started: Harness) -> None:
    create = started.interactions.commands["custom-command.create"][1]
    opening = FakeInteraction(
        guild_id=GUILD,
        user_id=STAFF,
        options={"name": "owned", "description": "Owned editor"},
    )
    await create(opening)
    token = opening.last.components[0].parts[0]
    foreign = FakeInteraction(
        guild_id=GUILD,
        user_id=STAFF + 1,
        custom_id=_custom_id(started, "editor_close", token),
    )
    await started.interactions.components[("button", "editor_close")](foreign)
    assert "belongs to someone else" in (foreign.last.content or "")
    assert (
        started.module._sessions.get(
            token, guild_id=GUILD, user_id=STAFF, now=started.module._clock()
        )
        is not None
    )


async def test_existing_editor_does_not_recreate_deleted_command(started: Harness) -> None:
    store = CommandStore(started.storage)
    command = await store.create(GUILD, "gone", "Gone", starter_document(), actor_id=STAFF, now=1)
    await started.module._sync_guild(GUILD)
    edit = started.interactions.commands["custom-command.edit"][1]
    opening = FakeInteraction(guild_id=GUILD, user_id=STAFF, options={"name": "gone"})
    await edit(opening)
    token = opening.last.components[0].parts[0]
    await store.delete(GUILD, "gone", expected_revision=command.revision)
    save = FakeInteraction(
        guild_id=GUILD, user_id=STAFF, custom_id=_custom_id(started, "editor_save", token)
    )
    await started.interactions.components[("button", "editor_save")](save)

    assert "deleted while" in (save.last.content or "")
    assert await store.get(GUILD, "gone") is None


async def test_direct_delete_defers_before_sync(started: Harness) -> None:
    store = CommandStore(started.storage)
    await store.create(GUILD, "delete-me", "Delete me", starter_document(), actor_id=STAFF, now=1)
    interaction = FakeInteraction(guild_id=GUILD, user_id=STAFF, options={"name": "delete-me"})
    await started.interactions.commands["custom-command.delete"][1](interaction)
    assert interaction.deferred is True
    assert interaction.last.content == "Deleted `/delete-me`."


async def test_llm_tool_posts_review_and_self_approval_applies(started: Harness) -> None:
    load, recorder = load_context(None)
    module = SPEC.create(load)
    # Use the started instance handlers to exercise its bound runtime.
    context = ModuleToolContext(STAFF, "Staff", GUILD, CHANNEL, None, TrustTier.STAFF)
    arguments = {
        "operation": "create",
        "name": "ai-command",
        "description": "Made by proposal",
        "content": document_to_dict(starter_document()),
        "summary": "Add a useful command",
    }
    output = await started.module.tool_propose(arguments, context)
    proposal_id = output.split()[1]

    assert len(started.discord.calls_for("send_message")) == 1
    controls = started.discord.calls_for("send_message")[0].kwargs["components"]
    assert {control.key for control in controls} == {
        "proposal_approve",
        "proposal_reject",
        "proposal_proposed",
    }
    preview = FakeInteraction(
        guild_id=GUILD,
        user_id=STAFF,
        custom_id=_custom_id(started, "proposal_proposed", proposal_id),
    )
    await started.interactions.components[("button", "proposal_proposed")](preview)
    assert preview.last.ephemeral is True
    assert preview.last.layout is not None
    approve = FakeInteraction(
        guild_id=GUILD,
        channel_id=CHANNEL,
        user_id=STAFF,
        custom_id=_custom_id(started, "proposal_approve", proposal_id),
    )
    await started.interactions.components[("button", "proposal_approve")](approve)

    assert approve.deferred is True
    assert (await CommandStore(started.storage).get(GUILD, "ai-command")) is not None
    assert (await CommandStore(started.storage).get_proposal(proposal_id)).state == "applied"  # type: ignore[union-attr]
    assert recorder.registry.tools["custom_commands_propose"].searchable
    schema = recorder.registry.tools["custom_commands_propose"].parameters
    content_schema = schema["properties"]["content"]
    assert content_schema["properties"]["version"] == {"const": 1}
    assert (
        len(
            content_schema["properties"]["pages"]["items"]["properties"]["blocks"]["items"]["oneOf"]
        )
        == 6
    )
    assert schema["allOf"][0]["then"]["required"] == ["description", "content"]
    await module.close()


async def test_replace_proposal_exposes_current_proposed_pages_and_is_guild_scoped(
    started: Harness,
) -> None:
    store = CommandStore(started.storage)
    await store.create(GUILD, "existing", "Existing", starter_document(), actor_id=STAFF, now=1)
    proposed = CommandDocument(
        pages=(
            Page("one", "One", blocks=(TextBlock(text="One"),)),
            Page("two", "Two", blocks=(TextBlock(text="Two"),)),
        )
    )
    context = ModuleToolContext(STAFF, "Staff", GUILD, CHANNEL, None, TrustTier.STAFF)
    output = await started.module.tool_propose(
        {
            "operation": "replace",
            "name": "existing",
            "description": "Replacement",
            "content": document_to_dict(proposed),
            "summary": "Replace it",
        },
        context,
    )
    proposal_id = output.split()[1]
    controls = started.discord.calls_for("send_message")[-1].kwargs["components"]
    assert {control.key for control in controls} == {
        "proposal_approve",
        "proposal_reject",
        "proposal_current",
        "proposal_proposed",
    }

    preview = FakeInteraction(
        guild_id=GUILD,
        user_id=STAFF,
        custom_id=_custom_id(started, "proposal_proposed", proposal_id),
    )
    await started.interactions.components[("button", "proposal_proposed")](preview)
    assert preview.last.components[0].key == "proposal_page"
    page = FakeInteraction(
        guild_id=GUILD,
        user_id=STAFF,
        custom_id=_custom_id(started, "proposal_page", proposal_id, "proposed"),
        values=("two",),
    )
    await started.interactions.components[("select", "proposal_page")](page)
    assert page.last.layout is not None
    assert isinstance(page.last.layout.items[0], LayoutText)
    assert page.last.layout.items[0].content == "Two"

    foreign = FakeInteraction(
        guild_id=GUILD + 1,
        user_id=STAFF,
        custom_id=_custom_id(started, "proposal_current", proposal_id),
    )
    await started.interactions.components[("button", "proposal_current")](foreign)
    assert "no longer available" in (foreign.last.content or "")

    rejected = FakeInteraction(
        guild_id=GUILD,
        user_id=STAFF,
        custom_id=_custom_id(started, "proposal_reject", proposal_id),
    )
    await started.interactions.components[("button", "proposal_reject")](rejected)
    assert rejected.deferred is True


async def test_list_is_bounded(started: Harness) -> None:
    store = CommandStore(started.storage)
    for index in range(30):
        await store.create(
            GUILD,
            f"command-{index}",
            "d" * 100,
            starter_document(),
            actor_id=STAFF,
            now=index,
        )
    listing = FakeInteraction(guild_id=GUILD, user_id=STAFF)
    await started.interactions.commands["custom-command.list"][1](listing)
    assert len(listing.last.content or "") <= 2_000
    assert "more command" in (listing.last.content or "")


async def test_session_store_expires_and_rejects_cross_guild() -> None:
    from kimi_agent_custom_commands.editor import EditorSession

    sessions = SessionStore()
    session = EditorSession.create(1, 2, "test", "Test", starter_document(), now=0)
    sessions.add(session)
    assert sessions.get(session.token, guild_id=2, user_id=2, now=1) is None
    assert sessions.get(session.token, guild_id=1, user_id=2, now=1_801) is None


async def test_startup_reconstructs_existing_guild_commands(started: Harness) -> None:
    await CommandStore(started.storage).create(
        GUILD, "restart", "After restart", starter_document(), actor_id=STAFF, now=1
    )
    await started.module.close()
    await started.module.start(started.ctx)

    assert "restart" in started.interactions.guild_commands[GUILD]
