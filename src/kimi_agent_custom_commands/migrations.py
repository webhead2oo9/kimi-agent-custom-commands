"""Forward-only module-owned schema."""

from __future__ import annotations

from kimi_agent_module_api import ScopedModuleMigration
from kimi_agent_module_api.contracts import MigrationContext


async def _create_commands(ctx: MigrationContext) -> None:
    commands = ctx.table("commands")
    proposals = ctx.table("proposals")
    await ctx.connection.execute(
        f"""
        CREATE TABLE {commands} (
            command_id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            description TEXT NOT NULL,
            content_json TEXT NOT NULL,
            revision INTEGER NOT NULL DEFAULT 1,
            created_by INTEGER NOT NULL,
            updated_by INTEGER NOT NULL,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            UNIQUE (guild_id, name)
        )
        """
    )
    await ctx.connection.execute(
        f"CREATE INDEX {ctx.table('commands_guild')} ON {commands} (guild_id, name)"
    )
    await ctx.connection.execute(
        f"""
        CREATE TABLE {proposals} (
            proposal_id TEXT PRIMARY KEY,
            guild_id INTEGER NOT NULL,
            command_id INTEGER,
            command_name TEXT NOT NULL,
            operation TEXT NOT NULL CHECK (operation IN ('create', 'replace', 'delete')),
            candidate_json TEXT,
            expected_revision INTEGER,
            state TEXT NOT NULL CHECK (state IN ('pending', 'applied', 'rejected')),
            proposer_id INTEGER NOT NULL,
            decider_id INTEGER,
            summary TEXT NOT NULL,
            decision_reason TEXT NOT NULL DEFAULT '',
            created_at REAL NOT NULL,
            decided_at REAL
        )
        """
    )


MIGRATIONS: tuple[ScopedModuleMigration, ...] = (("001_create_commands", _create_commands),)

__all__ = ["MIGRATIONS"]
