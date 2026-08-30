"""Custom-command lifecycle, editor, dynamic commands, and AI review flow."""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from collections.abc import Sequence
from dataclasses import replace
from typing import Any, cast

from kimi_agent_module_api import ModuleRuntimeContext, ModuleToolContext, ScopedModuleMigration
from kimi_agent_module_api.contracts import (
    ButtonSpec,
    CommandOption,
    CommandSpec,
    CommandSyncError,
    GuildCommand,
    LayoutText,
    ModuleContractError,
    ModuleInteraction,
    OutgoingEmbed,
    OutgoingLayout,
    Registration,
    SelectSpec,
    parse_custom_id,
)

from kimi_agent_custom_commands.editor import (
    EditorSession,
    SessionStore,
    block_fingerprint,
    block_from_values,
    block_modal,
    editor_components,
    editor_layout,
    general_modal,
    page_modal,
    reorder_modal,
)
from kimi_agent_custom_commands.migrations import MIGRATIONS
from kimi_agent_custom_commands.models import (
    Page,
    TextBlock,
    ValidationError,
    document_from_dict,
    document_to_dict,
    starter_document,
    validate_document,
    validate_identity,
)
from kimi_agent_custom_commands.renderer import page_selector, render_page
from kimi_agent_custom_commands.store import (
    CommandExists,
    CommandMissing,
    CommandStore,
    ProposalCandidate,
    RevisionConflict,
    StoredProposal,
)

log = logging.getLogger(__name__)
MODULE_NAME = "custom_commands"
COMMAND_GROUP = "custom-command"


class CustomCommandsModule:
    scoped_migrations: Sequence[ScopedModuleMigration] = MIGRATIONS

    def __init__(self, *, clock: Any = time.time) -> None:
        self._clock = clock
        self._ctx: ModuleRuntimeContext | None = None
        self._store: CommandStore | None = None
        self._registrations: list[Registration] = []
        self._sessions = SessionStore()
        self._guild_locks: dict[int, asyncio.Lock] = {}

    async def start(self, ctx: ModuleRuntimeContext) -> None:
        if self._ctx is not None:
            raise RuntimeError(f"{MODULE_NAME} is already started")
        self._ctx = ctx
        self._store = CommandStore(ctx.storage)
        self._register_management_commands(ctx)
        self._register_components(ctx)
        for guild_id in await self._store.guild_ids():
            if ctx.is_guild_active(guild_id):
                await self._sync_guild(guild_id)
        ctx.health.report("healthy")

    async def close(self) -> None:
        for registration in reversed(self._registrations):
            registration.close()
        self._registrations.clear()
        self._guild_locks.clear()
        self._sessions = SessionStore()
        self._store = None
        self._ctx = None

    def _register_management_commands(self, ctx: ModuleRuntimeContext) -> None:
        group_description = "Create and manage this server's custom slash commands."
        definitions = (
            (
                CommandSpec(
                    "create",
                    "Create a custom command.",
                    (
                        CommandOption("name", "string", "Slash command name.", required=True),
                        CommandOption(
                            "description", "string", "Discord command description.", required=True
                        ),
                    ),
                    "staff",
                    COMMAND_GROUP,
                    group_description,
                ),
                self._command_create,
                None,
            ),
            (
                CommandSpec(
                    "edit",
                    "Open the custom command editor.",
                    (
                        CommandOption(
                            "name", "string", "Command to edit.", required=True, autocomplete=True
                        ),
                    ),
                    "staff",
                    COMMAND_GROUP,
                    group_description,
                ),
                self._command_edit,
                self._autocomplete,
            ),
            (
                CommandSpec(
                    "delete",
                    "Delete a custom command.",
                    (
                        CommandOption(
                            "name", "string", "Command to delete.", required=True, autocomplete=True
                        ),
                    ),
                    "staff",
                    COMMAND_GROUP,
                    group_description,
                ),
                self._command_delete,
                self._autocomplete,
            ),
            (
                CommandSpec(
                    "list",
                    "List this server's custom commands.",
                    (),
                    "staff",
                    COMMAND_GROUP,
                    group_description,
                ),
                self._command_list,
                None,
            ),
        )
        for command, handler, autocomplete in definitions:
            self._registrations.append(
                ctx.interactions.add_command(command, handler, autocomplete=autocomplete)
            )

    def _register_components(self, ctx: ModuleRuntimeContext) -> None:
        staff_buttons = {
            "editor_general": self._editor_general,
            "editor_page_edit": self._editor_page_edit,
            "editor_page_add": self._editor_page_add,
            "editor_page_delete": self._editor_page_delete,
            "editor_block_edit": self._editor_block_edit,
            "editor_reorder": self._editor_reorder,
            "editor_block_delete": self._editor_block_delete,
            "editor_save": self._editor_save,
            "editor_discard": self._editor_discard,
            "editor_close": self._editor_close,
            "proposal_approve": self._proposal_approve,
            "proposal_reject": self._proposal_reject,
            "proposal_current": self._proposal_current,
            "proposal_proposed": self._proposal_proposed,
        }
        for key, handler in staff_buttons.items():
            self._registrations.append(
                ctx.interactions.register_component("button", key, handler, min_tier="staff")
            )
        for key, handler in {
            "editor_page": self._editor_select_page,
            "editor_block": self._editor_select_block,
            "editor_add_block": self._editor_add_block,
        }.items():
            self._registrations.append(
                ctx.interactions.register_component("select", key, handler, min_tier="staff")
            )
        self._registrations.append(
            ctx.interactions.register_component(
                "modal", "editor_modal", self._editor_modal, min_tier="staff"
            )
        )
        self._registrations.append(
            ctx.interactions.register_component(
                "select", "command_page", self._command_page, min_tier="member"
            )
        )
        self._registrations.append(
            ctx.interactions.register_component(
                "select", "proposal_page", self._proposal_page, min_tier="staff"
            )
        )

    async def _command_create(self, interaction: ModuleInteraction) -> None:
        name = str(interaction.options.get("name", "")).strip().lower()
        description = str(interaction.options.get("description", "")).strip()
        try:
            validate_identity(name, description)
            if await self._require_store().get(interaction.guild_id, name) is not None:
                raise CommandExists(name)
        except (ValidationError, CommandExists) as exc:
            await interaction.respond(f"Cannot create command: {exc}", ephemeral=True)
            return
        session = EditorSession.create(
            interaction.guild_id,
            interaction.user_id,
            name,
            description,
            starter_document(),
            now=self._clock(),
        )
        session.dirty = True
        self._sessions.add(session)
        await interaction.respond(
            layout=editor_layout(session), components=editor_components(session), ephemeral=True
        )

    async def _command_edit(self, interaction: ModuleInteraction) -> None:
        name = str(interaction.options.get("name", "")).strip().lower()
        command = await self._require_store().get(interaction.guild_id, name)
        if command is None:
            await interaction.respond("That command does not exist.", ephemeral=True)
            return
        session = EditorSession.create(
            interaction.guild_id,
            interaction.user_id,
            command.name,
            command.description,
            command.document,
            original=command,
            now=self._clock(),
        )
        self._sessions.add(session)
        await interaction.respond(
            layout=editor_layout(session), components=editor_components(session), ephemeral=True
        )

    async def _command_delete(self, interaction: ModuleInteraction) -> None:
        name = str(interaction.options.get("name", "")).strip().lower()
        await interaction.defer(ephemeral=True)
        async with self._lock(interaction.guild_id):
            try:
                previous = await self._require_store().delete(interaction.guild_id, name)
            except CommandMissing:
                await interaction.edit_original("That command does not exist.")
                return
            try:
                await self._sync_guild(interaction.guild_id)
            except ModuleContractError as exc:
                await self._require_store().compensate_command(previous, None)
                await interaction.edit_original(f"Discord rejected the change: {exc}")
                return
            except CommandSyncError:
                await interaction.edit_original(
                    "Deleted. Discord publication is pending a reconnect."
                )
                return
        await interaction.edit_original(f"Deleted `/{name}`.")

    async def _command_list(self, interaction: ModuleInteraction) -> None:
        commands = await self._require_store().list(interaction.guild_id)
        if not commands:
            await interaction.respond("This server has no custom commands.", ephemeral=True)
            return
        lines: list[str] = []
        used = 0
        for item in commands:
            line = f"`/{item.name}` — {item.description} (r{item.revision})"
            if used + len(line) + 1 > 1_850:
                break
            lines.append(line)
            used += len(line) + 1
        omitted = len(commands) - len(lines)
        if omitted:
            lines.append(f"…and {omitted} more command{'s' if omitted != 1 else ''}.")
        await interaction.respond("\n".join(lines), ephemeral=True)

    async def _autocomplete(
        self, interaction: ModuleInteraction, _option: str, current: str
    ) -> tuple[tuple[str, str], ...]:
        needle = current.casefold()
        commands = await self._require_store().list(interaction.guild_id)
        return tuple(
            (f"/{item.name}", item.name) for item in commands if needle in item.name.casefold()
        )[:25]

    async def _invoke(self, command_id: int, interaction: ModuleInteraction) -> None:
        command = await self._require_store().get_by_id(interaction.guild_id, command_id)
        if command is None:
            await interaction.respond("This command is no longer available.", ephemeral=True)
            return
        hidden = bool(interaction.options.get("hidden", False))
        selector = page_selector(command.command_id, command.document.pages)
        await interaction.respond(
            layout=render_page(command.document.pages[0]),
            ephemeral=hidden,
            components=(selector,) if selector else (),
        )

    async def _command_page(self, interaction: ModuleInteraction) -> None:
        parts = _parts(interaction)
        if len(parts) != 1 or not interaction.values:
            await interaction.respond("Invalid page selection.", ephemeral=True)
            return
        command = await self._require_store().get_by_id(interaction.guild_id, int(parts[0]))
        if command is None:
            await interaction.respond("This command is no longer available.", ephemeral=True)
            return
        index = next(
            (
                i
                for i, page in enumerate(command.document.pages)
                if page.key == interaction.values[0]
            ),
            -1,
        )
        if index < 0:
            await interaction.respond("That page no longer exists.", ephemeral=True)
            return
        selector = page_selector(command.command_id, command.document.pages, index)
        await interaction.edit_original(
            layout=render_page(command.document.pages[index]),
            components=(selector,) if selector else (),
        )

    async def _editor_general(self, interaction: ModuleInteraction) -> None:
        session = await self._session(interaction)
        if session:
            await interaction.show_modal(general_modal(session))

    async def _editor_page_edit(self, interaction: ModuleInteraction) -> None:
        session = await self._session(interaction, scoped_page=True)
        if session:
            await interaction.show_modal(page_modal(session))

    async def _editor_page_add(self, interaction: ModuleInteraction) -> None:
        session = await self._session(interaction)
        if session:
            if len(session.document.pages) >= 25:
                await interaction.respond(
                    "A command cannot have more than 25 pages.", ephemeral=True
                )
                return
            await interaction.show_modal(page_modal(session, adding=True))

    async def _editor_page_delete(self, interaction: ModuleInteraction) -> None:
        session = await self._session(interaction, scoped_page=True)
        if not session:
            return
        if len(session.document.pages) == 1:
            await interaction.respond("A command needs at least one page.", ephemeral=True)
            return
        pages = list(session.document.pages)
        pages.pop(session.selected_page)
        session.document = replace(session.document, pages=tuple(pages))
        session.set_page(min(session.selected_page, len(pages) - 1))
        session.dirty = True
        await self._repaint(interaction, session)

    async def _editor_select_page(self, interaction: ModuleInteraction) -> None:
        session = await self._session(interaction)
        if session and interaction.values:
            session.set_page(int(interaction.values[0]))
            await self._repaint(interaction, session)

    async def _editor_select_block(self, interaction: ModuleInteraction) -> None:
        session = await self._session(interaction, scoped_page=True)
        if session and interaction.values:
            session.selected_block = max(
                0, min(int(interaction.values[0]), len(session.page.blocks) - 1)
            )
            await self._repaint(interaction, session)

    async def _editor_add_block(self, interaction: ModuleInteraction) -> None:
        session = await self._session(interaction, scoped_page=True)
        if not session or not interaction.values:
            return
        if len(session.page.blocks) >= 25:
            await interaction.respond("A page cannot have more than 25 blocks.", ephemeral=True)
            return
        kind = interaction.values[0]
        modal = block_modal(session, kind, editing=False)
        if modal is not None:
            await interaction.show_modal(modal)
        else:
            session.replace_block(block_from_values(kind, {}), index=len(session.page.blocks))
            await self._repaint(interaction, session)

    async def _editor_block_edit(self, interaction: ModuleInteraction) -> None:
        session = await self._session(interaction, scoped_block=True)
        if not session:
            return
        kind = session.page.blocks[session.selected_block].type
        modal = block_modal(session, kind, editing=True)
        if modal is None:
            await interaction.respond("Dividers have no editable settings.", ephemeral=True)
        else:
            await interaction.show_modal(modal)

    async def _editor_reorder(self, interaction: ModuleInteraction) -> None:
        session = await self._session(interaction, scoped_block=True)
        if session:
            await interaction.show_modal(reorder_modal(session))

    async def _editor_block_delete(self, interaction: ModuleInteraction) -> None:
        session = await self._session(interaction, scoped_block=True)
        if not session:
            return
        parts = _parts(interaction)
        if len(parts) != 4 or parts[3] != block_fingerprint(
            session.page.blocks[session.selected_block]
        ):
            await interaction.respond(
                "The selected block changed; use the current editor controls.", ephemeral=True
            )
            return
        if len(session.page.blocks) == 1:
            await interaction.respond("A page needs at least one block.", ephemeral=True)
            return
        blocks = list(session.page.blocks)
        blocks.pop(session.selected_block)
        session.replace_page(replace(session.page, blocks=tuple(blocks)))
        session.selected_block = min(session.selected_block, len(blocks) - 1)
        await self._repaint(interaction, session)

    async def _editor_modal(self, interaction: ModuleInteraction) -> None:
        parts = _parts(interaction)
        if len(parts) < 2:
            await interaction.respond("This editor action is invalid.", ephemeral=True)
            return
        session = await self._session(interaction)
        if not session:
            return
        action = parts[1]
        values = dict(interaction.text_values)
        snapshot = (
            session.name,
            session.description,
            session.document,
            session.selected_page,
            session.selected_block,
            session.dirty,
        )
        try:
            if action == "general":
                validate_identity(
                    values.get("name", "").strip().lower(), values.get("description", "").strip()
                )
                session.name = values["name"].strip().lower()
                session.description = values["description"].strip()
                session.dirty = True
            elif action in {"page_add", "page_edit"}:
                self._apply_page_modal(session, parts, values)
            elif action in {"block_add", "block_edit"}:
                self._apply_block_modal(session, parts, values)
            elif action == "reorder":
                self._apply_reorder_modal(session, parts, values)
            validate_document(session.document)
        except (ValidationError, ValueError) as exc:
            (
                session.name,
                session.description,
                session.document,
                session.selected_page,
                session.selected_block,
                session.dirty,
            ) = snapshot
            await interaction.respond(f"That edit is invalid: {exc}", ephemeral=True)
            return
        await self._repaint(interaction, session)

    def _apply_page_modal(
        self, session: EditorSession, parts: tuple[str, ...], values: dict[str, str]
    ) -> None:
        action = parts[1]
        if action == "page_edit" and (len(parts) < 3 or parts[2] != session.page.key):
            raise ValueError("the selected page changed; reopen the form")
        color_text = values.get("accent_color", "").strip().removeprefix("#")
        color = int(color_text, 16) if color_text else None
        page = Page(
            key=values.get("key", "").strip().lower(),
            label=values.get("label", "").strip(),
            description=values.get("description", "").strip(),
            accent_color=color,
            thumbnail_url=values.get("thumbnail_url", "").strip() or None,
            blocks=(TextBlock(text="New page"),) if action == "page_add" else session.page.blocks,
        )
        if action == "page_add":
            pages = (*session.document.pages, page)
            session.document = replace(session.document, pages=pages)
            session.set_page(len(pages) - 1)
            session.dirty = True
        else:
            session.replace_page(page)

    def _apply_block_modal(
        self, session: EditorSession, parts: tuple[str, ...], values: dict[str, str]
    ) -> None:
        action = parts[1]
        expected_length = 5 if action == "block_edit" else 6
        if len(parts) != expected_length or parts[2] != session.page.key:
            raise ValueError("the selected page changed; reopen the form")
        expected_index = int(parts[3])
        if action == "block_edit" and (
            expected_index != session.selected_block
            or not 0 <= expected_index < len(session.page.blocks)
        ):
            raise ValueError("the selected block changed; reopen the form")
        if action == "block_edit" and parts[4] != block_fingerprint(
            session.page.blocks[expected_index]
        ):
            raise ValueError("the selected block changed; reopen the form")
        kind = session.page.blocks[expected_index].type if action == "block_edit" else parts[4]
        target = expected_index if action == "block_edit" else len(session.page.blocks)
        session.replace_block(block_from_values(kind, values), index=target)

    def _apply_reorder_modal(
        self, session: EditorSession, parts: tuple[str, ...], values: dict[str, str]
    ) -> None:
        if (
            len(parts) != 5
            or parts[2] != session.page.key
            or int(parts[3]) != session.selected_block
            or parts[4] != block_fingerprint(session.page.blocks[session.selected_block])
        ):
            raise ValueError("the selected page or block changed; reopen the form")
        page_target = int(values.get("page_position", "")) - 1
        block_target = int(values.get("block_position", "")) - 1
        if not 0 <= page_target < len(session.document.pages):
            raise ValueError("page position is out of range")
        if not 0 <= block_target < len(session.page.blocks):
            raise ValueError("block position is out of range")
        original_page_index = session.selected_page
        pages = list(session.document.pages)
        page = pages.pop(original_page_index)
        blocks = list(page.blocks)
        block = blocks.pop(session.selected_block)
        blocks.insert(block_target, block)
        page = replace(page, blocks=tuple(blocks))
        pages.insert(page_target, page)
        session.document = replace(session.document, pages=tuple(pages))
        session.selected_page = page_target
        session.selected_block = block_target
        session.dirty = True

    async def _editor_save(self, interaction: ModuleInteraction) -> None:
        session = await self._session(interaction)
        if not session:
            return
        try:
            validate_identity(session.name, session.description)
            validate_document(session.document)
        except ValidationError as exc:
            await interaction.respond(f"Cannot save: {exc}", ephemeral=True)
            return
        await interaction.defer(ephemeral=True)
        async with self._lock(session.guild_id):
            previous = (
                await self._require_store().get_by_id(session.guild_id, session.command_id)
                if session.command_id is not None
                else None
            )
            if session.command_id is not None and previous is None:
                await interaction.follow_up(
                    "Cannot save: the command was deleted while you were editing it.",
                    ephemeral=True,
                )
                return
            try:
                if session.command_id is None:
                    current = await self._require_store().create(
                        session.guild_id,
                        session.name,
                        session.description,
                        session.document,
                        actor_id=session.user_id,
                        now=self._clock(),
                    )
                    registration_changed = True
                else:
                    assert previous is not None
                    current = await self._require_store().update(
                        previous.command_id,
                        session.guild_id,
                        session.name,
                        session.description,
                        session.document,
                        expected_revision=cast(int, session.baseline_revision),
                        actor_id=session.user_id,
                        now=self._clock(),
                    )
                    registration_changed = (
                        previous.name != current.name or previous.description != current.description
                    )
            except (CommandExists, RevisionConflict) as exc:
                await interaction.follow_up(
                    f"Cannot save: {exc}. Reload the editor.", ephemeral=True
                )
                return
            pending = False
            if registration_changed:
                try:
                    await self._sync_guild(session.guild_id)
                except CommandSyncError:
                    pending = True
                except ModuleContractError as exc:
                    await self._require_store().compensate_command(previous, current)
                    await interaction.follow_up(
                        f"Discord rejected the change: {exc}", ephemeral=True
                    )
                    return
        self._sessions.remove(session.token)
        suffix = " Discord publication is pending a reconnect." if pending else ""
        await interaction.edit_original(
            layout=_status_layout(f"Saved `/{current.name}`.{suffix}"), components=()
        )

    async def _editor_discard(self, interaction: ModuleInteraction) -> None:
        session = await self._session(interaction)
        if not session:
            return
        if session.command_id is None:
            self._sessions.remove(session.token)
            await interaction.edit_original(
                layout=_status_layout("Discarded the new command."), components=()
            )
            return
        command = await self._require_store().get_by_id(session.guild_id, session.command_id)
        if command is None:
            self._sessions.remove(session.token)
            await interaction.edit_original(
                layout=_status_layout("The command was deleted while you were editing it."),
                components=(),
            )
            return
        session.name, session.description, session.document = (
            command.name,
            command.description,
            command.document,
        )
        session.baseline_revision = command.revision
        session.set_page(0)
        session.dirty = False
        await self._repaint(interaction, session)

    async def _editor_close(self, interaction: ModuleInteraction) -> None:
        session = await self._session(interaction)
        if not session:
            return
        self._sessions.remove(session.token)
        await interaction.edit_original(
            layout=_status_layout("Editor closed without saving further changes."), components=()
        )

    async def _session(
        self,
        interaction: ModuleInteraction,
        *,
        scoped_page: bool = False,
        scoped_block: bool = False,
    ) -> EditorSession | None:
        parts = _parts(interaction)
        if not parts:
            await interaction.respond("This editor action is invalid.", ephemeral=True)
            return None
        session = self._sessions.get(
            parts[0], guild_id=interaction.guild_id, user_id=interaction.user_id, now=self._clock()
        )
        if session is None:
            await interaction.respond(
                "This editor expired or belongs to someone else.", ephemeral=True
            )
            return None
        if scoped_page and (len(parts) < 2 or parts[1] != session.page.key):
            await interaction.respond(
                "The selected page changed; use the current editor controls.", ephemeral=True
            )
            return None
        if scoped_block and (
            len(parts) < 3
            or parts[1] != session.page.key
            or int(parts[2]) != session.selected_block
        ):
            await interaction.respond(
                "The selected block changed; use the current editor controls.", ephemeral=True
            )
            return None
        return session

    async def _repaint(self, interaction: ModuleInteraction, session: EditorSession) -> None:
        await interaction.edit_original(
            layout=editor_layout(session), components=editor_components(session)
        )

    async def tool_read(self, arguments: dict[str, Any], context: ModuleToolContext) -> str:
        if context.guild_id is None:
            return "This tool is only available in a server."
        commands = await self._require_store().list(context.guild_id)
        query = str(arguments.get("query", "")).strip().lower()
        selected = [item for item in commands if not query or query in item.name.lower()]
        if len(selected) == 1 and selected[0].name == query:
            item = selected[0]
            return json.dumps(
                {
                    "id": item.command_id,
                    "name": item.name,
                    "description": item.description,
                    "revision": item.revision,
                    "content": document_to_dict(item.document),
                },
                ensure_ascii=False,
            )
        return json.dumps(
            [
                {"name": item.name, "description": item.description, "revision": item.revision}
                for item in selected[:50]
            ],
            ensure_ascii=False,
        )

    async def tool_propose(self, arguments: dict[str, Any], context: ModuleToolContext) -> str:
        if context.guild_id is None or context.channel_id is None:
            return "This tool must be used from a server channel."
        operation = str(arguments.get("operation", ""))
        name = str(arguments.get("name", "")).strip().lower()
        summary = str(arguments.get("summary", "")).strip()[:500]
        if operation not in {"create", "replace", "delete"}:
            return "operation must be create, replace, or delete"
        store = self._require_store()
        async with self._lock(context.guild_id):
            existing = await store.get(context.guild_id, name)
            try:
                candidate = None
                if operation != "delete":
                    description = str(arguments.get("description", "")).strip()
                    document = document_from_dict(arguments.get("content"))
                    candidate_name = (
                        name
                        if operation == "create"
                        else str(arguments.get("new_name", name)).strip().lower()
                    )
                    candidate = ProposalCandidate(candidate_name, description, document)
                proposal = await store.create_proposal(
                    proposal_id=uuid.uuid4().hex,
                    guild_id=context.guild_id,
                    command=existing,
                    command_name=name,
                    operation=cast(Any, operation),
                    candidate=candidate,
                    proposer_id=context.user_id,
                    summary=summary or f"{operation} /{name}",
                    now=self._clock(),
                )
            except (ValidationError, CommandExists, CommandMissing, ValueError) as exc:
                return f"Proposal rejected before review: {exc}"
        try:
            await self._require_ctx().discord.send_message(
                context.channel_id,
                embed=_proposal_embed(proposal),
                components=_proposal_controls(proposal),
            )
        except Exception as exc:
            async with self._lock(context.guild_id):
                await store.discard_pending_proposal(proposal.proposal_id)
            log.exception("Could not post custom-command proposal")
            return f"Could not post the review card: {exc}"
        return f"Proposal {proposal.proposal_id} posted for staff review in this channel."

    async def _proposal_current(self, interaction: ModuleInteraction) -> None:
        await self._show_proposal_preview(interaction, "current")

    async def _proposal_proposed(self, interaction: ModuleInteraction) -> None:
        await self._show_proposal_preview(interaction, "proposed")

    async def _show_proposal_preview(self, interaction: ModuleInteraction, source: str) -> None:
        parts = _parts(interaction)
        result = (
            await self._proposal_preview(interaction.guild_id, parts[0], source)
            if len(parts) == 1
            else None
        )
        if result is None:
            await interaction.respond("That preview is no longer available.", ephemeral=True)
            return
        document = result
        selector = _proposal_page_selector(parts[0], source, document.pages)
        await interaction.respond(
            layout=render_page(document.pages[0]),
            components=(selector,) if selector else (),
            ephemeral=True,
        )

    async def _proposal_page(self, interaction: ModuleInteraction) -> None:
        parts = _parts(interaction)
        document = (
            await self._proposal_preview(interaction.guild_id, parts[0], parts[1])
            if len(parts) == 2 and interaction.values
            else None
        )
        if document is None:
            await interaction.respond("That preview is no longer available.", ephemeral=True)
            return
        index = next(
            (i for i, page in enumerate(document.pages) if page.key == interaction.values[0]), -1
        )
        if index < 0:
            await interaction.respond("That page no longer exists.", ephemeral=True)
            return
        selector = _proposal_page_selector(parts[0], parts[1], document.pages, index)
        await interaction.edit_original(
            layout=render_page(document.pages[index]),
            components=(selector,) if selector else (),
        )

    async def _proposal_preview(self, guild_id: int, proposal_id: str, source: str) -> Any | None:
        proposal = await self._require_store().get_proposal(proposal_id)
        if proposal is None or proposal.guild_id != guild_id:
            return None
        if source == "proposed":
            return proposal.candidate.document if proposal.candidate else None
        if source != "current" or proposal.command_id is None:
            return None
        command = await self._require_store().get_by_id(guild_id, proposal.command_id)
        return command.document if command else None

    async def _proposal_approve(self, interaction: ModuleInteraction) -> None:
        parts = _parts(interaction)
        if len(parts) != 1:
            await interaction.respond("Invalid proposal.", ephemeral=True)
            return
        await interaction.defer(ephemeral=True)
        async with self._lock(interaction.guild_id):
            try:
                result = await self._require_store().approve_proposal(
                    parts[0],
                    guild_id=interaction.guild_id,
                    decider_id=interaction.user_id,
                    now=self._clock(),
                )
            except CommandMissing:
                await interaction.follow_up("Proposal not found in this server.", ephemeral=True)
                return
            if not result.applied:
                await interaction.edit_original(
                    embed=_proposal_embed(await self._require_proposal(parts[0]), result.reason),
                    components=(),
                )
                return
            registration_changed = result.proposal.operation != "replace" or (
                result.previous is not None
                and result.current is not None
                and (
                    result.previous.name != result.current.name
                    or result.previous.description != result.current.description
                )
            )
            pending = False
            if registration_changed:
                try:
                    await self._sync_guild(interaction.guild_id)
                except CommandSyncError:
                    pending = True
                except ModuleContractError as exc:
                    reason = f"Discord rejected the command: {exc}"
                    await self._require_store().compensate_approval(
                        result, decider_id=interaction.user_id, reason=reason, now=self._clock()
                    )
                    await interaction.edit_original(
                        embed=_proposal_embed(await self._require_proposal(parts[0]), reason),
                        components=(),
                    )
                    return
        detail = (
            "Approved; Discord publication is pending a reconnect."
            if pending
            else "Approved and applied."
        )
        await interaction.edit_original(
            embed=_proposal_embed(await self._require_proposal(parts[0]), detail), components=()
        )

    async def _proposal_reject(self, interaction: ModuleInteraction) -> None:
        parts = _parts(interaction)
        if len(parts) != 1:
            await interaction.respond("Invalid proposal.", ephemeral=True)
            return
        await interaction.defer(ephemeral=True)
        async with self._lock(interaction.guild_id):
            proposal = await self._require_store().get_proposal(parts[0])
            if proposal is None or proposal.guild_id != interaction.guild_id:
                await interaction.follow_up("Proposal not found in this server.", ephemeral=True)
                return
            changed = await self._require_store().mark_proposal(
                parts[0],
                state="rejected",
                decider_id=interaction.user_id,
                reason="Rejected by staff",
                now=self._clock(),
            )
        detail = "Rejected." if changed else f"Already {proposal.state}."
        await interaction.edit_original(
            embed=_proposal_embed(await self._require_proposal(parts[0]), detail), components=()
        )

    async def _sync_guild(self, guild_id: int) -> None:
        commands = await self._require_store().list(guild_id)
        desired = tuple(
            GuildCommand(
                CommandSpec(
                    item.name,
                    item.description,
                    (CommandOption("hidden", "boolean", "Only show the response to you."),),
                    min_tier="member",
                ),
                _handler_for(self, item.command_id),
            )
            for item in commands
        )
        await self._require_ctx().interactions.replace_guild_commands(guild_id, desired)

    def _lock(self, guild_id: int) -> asyncio.Lock:
        return self._guild_locks.setdefault(guild_id, asyncio.Lock())

    def _require_ctx(self) -> ModuleRuntimeContext:
        if self._ctx is None:
            raise RuntimeError(f"{MODULE_NAME} is not started")
        return self._ctx

    def _require_store(self) -> CommandStore:
        if self._store is None:
            raise RuntimeError(f"{MODULE_NAME} is not started")
        return self._store

    async def _require_proposal(self, proposal_id: str) -> StoredProposal:
        proposal = await self._require_store().get_proposal(proposal_id)
        if proposal is None:
            raise RuntimeError("proposal disappeared")
        return proposal


def _handler_for(module: CustomCommandsModule, command_id: int) -> Any:
    async def handler(interaction: ModuleInteraction) -> None:
        await module._invoke(command_id, interaction)

    return handler


def _status_layout(content: str) -> OutgoingLayout:
    return OutgoingLayout((LayoutText(content),))


def _parts(interaction: ModuleInteraction) -> tuple[str, ...]:
    if interaction.custom_id is None:
        return ()
    parsed = parse_custom_id(interaction.custom_id)
    return parsed[2] if parsed is not None else ()


def _proposal_embed(
    proposal: StoredProposal, status: str = "Pending staff review"
) -> OutgoingEmbed:
    candidate = proposal.candidate
    target = f"/{proposal.command_name}"
    if candidate and candidate.name != proposal.command_name:
        target += f" → /{candidate.name}"
    fields = [
        ("Operation", proposal.operation.title(), True),
        ("Target", target, True),
        ("Status", status, False),
        ("Summary", proposal.summary or "No summary provided.", False),
    ]
    if candidate:
        fields.append(
            (
                "Candidate",
                f"{candidate.description}\n{len(candidate.document.pages)} page(s)",
                False,
            )
        )
    return OutgoingEmbed(title="Custom command proposal", color=0x5865F2, fields=tuple(fields))


def _proposal_controls(proposal: StoredProposal) -> tuple[ButtonSpec, ...]:
    controls = [
        ButtonSpec("proposal_approve", "Approve", "success", (proposal.proposal_id,)),
        ButtonSpec("proposal_reject", "Reject", "danger", (proposal.proposal_id,)),
    ]
    if proposal.command_id is not None:
        controls.append(ButtonSpec("proposal_current", "Current", parts=(proposal.proposal_id,)))
    if proposal.candidate is not None:
        controls.append(ButtonSpec("proposal_proposed", "Proposed", parts=(proposal.proposal_id,)))
    return tuple(controls)


def _proposal_page_selector(
    proposal_id: str, source: str, pages: tuple[Page, ...], selected: int = 0
) -> SelectSpec | None:
    if len(pages) < 2:
        return None
    return SelectSpec(
        "proposal_page",
        tuple(
            (
                f"✓ {page.label[:98]}" if index == selected else page.label,
                page.key,
                page.description or None,
            )
            for index, page in enumerate(pages)
        ),
        "Choose a preview page",
        (proposal_id, source),
    )


__all__ = ["COMMAND_GROUP", "MODULE_NAME", "CustomCommandsModule"]
