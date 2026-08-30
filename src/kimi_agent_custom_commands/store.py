"""Transactional persistence for commands and AI proposals."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Literal

from kimi_agent_module_api.contracts import ModuleStorage

from kimi_agent_custom_commands.models import (
    CommandDocument,
    StoredCommand,
    document_from_dict,
    document_to_dict,
    validate_document,
    validate_identity,
)


class RevisionConflict(RuntimeError):
    pass


class CommandExists(RuntimeError):
    pass


class CommandMissing(RuntimeError):
    pass


ProposalOperation = Literal["create", "replace", "delete"]
ProposalState = Literal["pending", "applied", "rejected"]


@dataclass(frozen=True, slots=True)
class ProposalCandidate:
    name: str
    description: str
    document: CommandDocument


@dataclass(frozen=True, slots=True)
class StoredProposal:
    proposal_id: str
    guild_id: int
    command_id: int | None
    command_name: str
    operation: ProposalOperation
    candidate: ProposalCandidate | None
    expected_revision: int | None
    state: ProposalState
    proposer_id: int
    decider_id: int | None
    summary: str
    decision_reason: str
    created_at: float
    decided_at: float | None


@dataclass(frozen=True, slots=True)
class ApprovalResult:
    proposal: StoredProposal
    applied: bool
    reason: str
    previous: StoredCommand | None = None
    current: StoredCommand | None = None


class CommandStore:
    def __init__(self, storage: ModuleStorage) -> None:
        self._storage = storage
        self._commands = storage.table("commands")
        self._proposals = storage.table("proposals")

    async def guild_ids(self) -> tuple[int, ...]:
        cursor = await self._storage.connection.execute(
            f"SELECT DISTINCT guild_id FROM {self._commands} ORDER BY guild_id"
        )
        return tuple(int(row[0]) for row in await cursor.fetchall())

    async def list(self, guild_id: int) -> tuple[StoredCommand, ...]:
        cursor = await self._storage.connection.execute(
            f"SELECT {_COMMAND_COLUMNS} FROM {self._commands} WHERE guild_id = ? ORDER BY name",
            (guild_id,),
        )
        return tuple(_command_from_row(row) for row in await cursor.fetchall())

    async def get(self, guild_id: int, name: str) -> StoredCommand | None:
        cursor = await self._storage.connection.execute(
            f"SELECT {_COMMAND_COLUMNS} FROM {self._commands} WHERE guild_id = ? AND name = ?",
            (guild_id, name),
        )
        row = await cursor.fetchone()
        return _command_from_row(row) if row is not None else None

    async def get_by_id(self, guild_id: int, command_id: int) -> StoredCommand | None:
        cursor = await self._storage.connection.execute(
            f"SELECT {_COMMAND_COLUMNS} FROM {self._commands} "
            "WHERE guild_id = ? AND command_id = ?",
            (guild_id, command_id),
        )
        row = await cursor.fetchone()
        return _command_from_row(row) if row is not None else None

    async def create(
        self,
        guild_id: int,
        name: str,
        description: str,
        document: CommandDocument,
        *,
        actor_id: int,
        now: float,
    ) -> StoredCommand:
        validate_identity(name, description)
        validate_document(document)
        payload = _document_json(document)
        try:
            async with self._storage.write_transaction() as connection:
                cursor = await connection.execute(
                    f"INSERT INTO {self._commands} "
                    "(guild_id, name, description, content_json, revision, created_by, "
                    "updated_by, created_at, updated_at) VALUES (?, ?, ?, ?, 1, ?, ?, ?, ?)",
                    (guild_id, name, description, payload, actor_id, actor_id, now, now),
                )
                command_id = int(cursor.lastrowid)
        except Exception as exc:
            if "UNIQUE constraint failed" in str(exc):
                raise CommandExists(name) from exc
            raise
        command = await self.get_by_id(guild_id, command_id)
        assert command is not None
        return command

    async def update(
        self,
        command_id: int,
        guild_id: int,
        name: str,
        description: str,
        document: CommandDocument,
        *,
        expected_revision: int,
        actor_id: int,
        now: float,
    ) -> StoredCommand:
        validate_identity(name, description)
        validate_document(document)
        try:
            async with self._storage.write_transaction() as connection:
                cursor = await connection.execute(
                    f"UPDATE {self._commands} SET name = ?, description = ?, content_json = ?, "
                    "revision = revision + 1, updated_by = ?, updated_at = ? "
                    "WHERE command_id = ? AND guild_id = ? AND revision = ?",
                    (
                        name,
                        description,
                        _document_json(document),
                        actor_id,
                        now,
                        command_id,
                        guild_id,
                        expected_revision,
                    ),
                )
                if cursor.rowcount != 1:
                    raise RevisionConflict(name)
        except Exception as exc:
            if "UNIQUE constraint failed" in str(exc):
                raise CommandExists(name) from exc
            raise
        command = await self.get_by_id(guild_id, command_id)
        assert command is not None
        return command

    async def delete(
        self, guild_id: int, name: str, *, expected_revision: int | None = None
    ) -> StoredCommand:
        command = await self.get(guild_id, name)
        if command is None:
            raise CommandMissing(name)
        if expected_revision is not None and command.revision != expected_revision:
            raise RevisionConflict(name)
        async with self._storage.write_transaction() as connection:
            cursor = await connection.execute(
                f"DELETE FROM {self._commands} WHERE guild_id = ? AND command_id = ? AND revision = ?",
                (guild_id, command.command_id, command.revision),
            )
            if cursor.rowcount != 1:
                raise RevisionConflict(name)
        return command

    async def compensate_command(
        self, previous: StoredCommand | None, current: StoredCommand | None
    ) -> None:
        """Restore a direct mutation after deterministic registration rejection."""
        if previous is None and current is None:
            return
        async with self._storage.write_transaction() as connection:
            await _restore_command(connection, self._commands, previous, current)

    async def create_proposal(
        self,
        *,
        proposal_id: str,
        guild_id: int,
        command: StoredCommand | None,
        command_name: str,
        operation: ProposalOperation,
        candidate: ProposalCandidate | None,
        proposer_id: int,
        summary: str,
        now: float,
    ) -> StoredProposal:
        if operation == "create" and command is not None:
            raise CommandExists(command_name)
        if operation != "create" and command is None:
            raise CommandMissing(command_name)
        if candidate is not None:
            validate_identity(candidate.name, candidate.description)
            validate_document(candidate.document)
        candidate_json = _candidate_json(candidate) if candidate is not None else None
        async with self._storage.write_transaction() as connection:
            await connection.execute(
                f"INSERT INTO {self._proposals} "
                "(proposal_id, guild_id, command_id, command_name, operation, candidate_json, "
                "expected_revision, state, proposer_id, summary, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?)",
                (
                    proposal_id,
                    guild_id,
                    command.command_id if command else None,
                    command_name,
                    operation,
                    candidate_json,
                    command.revision if command else None,
                    proposer_id,
                    summary,
                    now,
                ),
            )
        proposal = await self.get_proposal(proposal_id)
        assert proposal is not None
        return proposal

    async def discard_pending_proposal(self, proposal_id: str) -> None:
        async with self._storage.write_transaction() as connection:
            await connection.execute(
                f"DELETE FROM {self._proposals} WHERE proposal_id = ? AND state = 'pending'",
                (proposal_id,),
            )

    async def get_proposal(self, proposal_id: str) -> StoredProposal | None:
        cursor = await self._storage.connection.execute(
            f"SELECT {_PROPOSAL_COLUMNS} FROM {self._proposals} WHERE proposal_id = ?",
            (proposal_id,),
        )
        row = await cursor.fetchone()
        return _proposal_from_row(row) if row is not None else None

    async def mark_proposal(
        self,
        proposal_id: str,
        *,
        state: Literal["applied", "rejected"],
        decider_id: int,
        reason: str,
        now: float,
    ) -> bool:
        async with self._storage.write_transaction() as connection:
            cursor = await connection.execute(
                f"UPDATE {self._proposals} SET state = ?, decider_id = ?, decision_reason = ?, "
                "decided_at = ? WHERE proposal_id = ? AND state = 'pending'",
                (state, decider_id, reason, now, proposal_id),
            )
            return int(cursor.rowcount or 0) == 1

    async def approve_proposal(
        self, proposal_id: str, *, guild_id: int, decider_id: int, now: float
    ) -> ApprovalResult:
        """CAS the proposal and target command in one transaction."""
        async with self._storage.write_transaction() as connection:
            cursor = await connection.execute(
                f"SELECT {_PROPOSAL_COLUMNS} FROM {self._proposals} WHERE proposal_id = ?",
                (proposal_id,),
            )
            row = await cursor.fetchone()
            if row is None:
                raise CommandMissing("proposal")
            proposal = _proposal_from_row(row)
            if proposal.guild_id != guild_id:
                raise CommandMissing("proposal")
            if proposal.state != "pending":
                return ApprovalResult(proposal, False, f"proposal is already {proposal.state}")

            previous = await _select_command(
                connection, self._commands, guild_id, proposal.command_name
            )
            conflict = _proposal_conflict(proposal, previous)
            if (
                conflict is None
                and proposal.operation == "replace"
                and proposal.candidate is not None
            ):
                candidate_owner = await _select_command(
                    connection, self._commands, guild_id, proposal.candidate.name
                )
                if (
                    candidate_owner is not None
                    and candidate_owner.command_id != proposal.command_id
                ):
                    conflict = f"/{proposal.candidate.name} is already owned by another command"
            if conflict is not None:
                await _mark_proposal_with_connection(
                    connection, self._proposals, proposal_id, "rejected", decider_id, conflict, now
                )
                return ApprovalResult(proposal, False, conflict, previous=previous)

            candidate = proposal.candidate
            current: StoredCommand | None = None
            if proposal.operation == "create":
                assert candidate is not None
                cursor = await connection.execute(
                    f"INSERT INTO {self._commands} "
                    "(guild_id, name, description, content_json, revision, created_by, updated_by, "
                    "created_at, updated_at) VALUES (?, ?, ?, ?, 1, ?, ?, ?, ?)",
                    (
                        guild_id,
                        candidate.name,
                        candidate.description,
                        _document_json(candidate.document),
                        decider_id,
                        decider_id,
                        now,
                        now,
                    ),
                )
                current = StoredCommand(
                    int(cursor.lastrowid),
                    guild_id,
                    candidate.name,
                    candidate.description,
                    candidate.document,
                    1,
                    decider_id,
                    decider_id,
                    now,
                    now,
                )
            elif proposal.operation == "replace":
                assert candidate is not None and previous is not None
                cursor = await connection.execute(
                    f"UPDATE {self._commands} SET name = ?, description = ?, content_json = ?, "
                    "revision = revision + 1, updated_by = ?, updated_at = ? "
                    "WHERE guild_id = ? AND command_id = ? AND revision = ?",
                    (
                        candidate.name,
                        candidate.description,
                        _document_json(candidate.document),
                        decider_id,
                        now,
                        guild_id,
                        previous.command_id,
                        previous.revision,
                    ),
                )
                if cursor.rowcount != 1:
                    raise RevisionConflict(proposal.command_name)
                current = StoredCommand(
                    previous.command_id,
                    guild_id,
                    candidate.name,
                    candidate.description,
                    candidate.document,
                    previous.revision + 1,
                    previous.created_by,
                    decider_id,
                    previous.created_at,
                    now,
                )
            else:
                assert previous is not None
                cursor = await connection.execute(
                    f"DELETE FROM {self._commands} "
                    "WHERE guild_id = ? AND command_id = ? AND revision = ?",
                    (guild_id, previous.command_id, previous.revision),
                )
                if cursor.rowcount != 1:
                    raise RevisionConflict(proposal.command_name)

            await _mark_proposal_with_connection(
                connection, self._proposals, proposal_id, "applied", decider_id, "", now
            )
            return ApprovalResult(proposal, True, "", previous=previous, current=current)

    async def compensate_approval(
        self,
        result: ApprovalResult,
        *,
        decider_id: int,
        reason: str,
        now: float,
    ) -> None:
        """Restore the exact prior command and reject after deterministic sync failure."""
        proposal = result.proposal
        async with self._storage.write_transaction() as connection:
            await _restore_command(connection, self._commands, result.previous, result.current)
            await connection.execute(
                f"UPDATE {self._proposals} SET state = 'rejected', decider_id = ?, "
                "decision_reason = ?, decided_at = ? WHERE proposal_id = ? AND state = 'applied'",
                (decider_id, reason, now, proposal.proposal_id),
            )


_COMMAND_COLUMNS = (
    "command_id, guild_id, name, description, content_json, revision, "
    "created_by, updated_by, created_at, updated_at"
)
_PROPOSAL_COLUMNS = (
    "proposal_id, guild_id, command_id, command_name, operation, candidate_json, "
    "expected_revision, state, proposer_id, decider_id, summary, decision_reason, "
    "created_at, decided_at"
)


def _document_json(document: CommandDocument) -> str:
    return json.dumps(document_to_dict(document), ensure_ascii=False, separators=(",", ":"))


def _command_parameters(command: StoredCommand) -> tuple[object, ...]:
    return (
        command.command_id,
        command.guild_id,
        command.name,
        command.description,
        _document_json(command.document),
        command.revision,
        command.created_by,
        command.updated_by,
        command.created_at,
        command.updated_at,
    )


def _command_from_row(row: Any) -> StoredCommand:
    return StoredCommand(
        command_id=int(row[0]),
        guild_id=int(row[1]),
        name=str(row[2]),
        description=str(row[3]),
        document=document_from_dict(json.loads(row[4])),
        revision=int(row[5]),
        created_by=int(row[6]),
        updated_by=int(row[7]),
        created_at=float(row[8]),
        updated_at=float(row[9]),
    )


def _candidate_json(candidate: ProposalCandidate) -> str:
    return json.dumps(
        {
            "name": candidate.name,
            "description": candidate.description,
            "document": document_to_dict(candidate.document),
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _candidate_from_json(payload: str) -> ProposalCandidate:
    value = json.loads(payload)
    if not isinstance(value, dict):
        raise TypeError("invalid proposal candidate")
    candidate = ProposalCandidate(
        name=str(value["name"]),
        description=str(value["description"]),
        document=document_from_dict(value["document"]),
    )
    validate_identity(candidate.name, candidate.description)
    return candidate


def _proposal_from_row(row: Any) -> StoredProposal:
    return StoredProposal(
        proposal_id=str(row[0]),
        guild_id=int(row[1]),
        command_id=int(row[2]) if row[2] is not None else None,
        command_name=str(row[3]),
        operation=row[4],
        candidate=_candidate_from_json(row[5]) if row[5] is not None else None,
        expected_revision=int(row[6]) if row[6] is not None else None,
        state=row[7],
        proposer_id=int(row[8]),
        decider_id=int(row[9]) if row[9] is not None else None,
        summary=str(row[10]),
        decision_reason=str(row[11]),
        created_at=float(row[12]),
        decided_at=float(row[13]) if row[13] is not None else None,
    )


async def _restore_command(
    connection: Any,
    table: str,
    previous: StoredCommand | None,
    current: StoredCommand | None,
) -> None:
    if previous is None:
        assert current is not None
        await connection.execute(
            f"DELETE FROM {table} WHERE guild_id = ? AND command_id = ?",
            (current.guild_id, current.command_id),
        )
    elif current is None:
        await connection.execute(
            f"INSERT INTO {table} "
            "(command_id, guild_id, name, description, content_json, revision, created_by, "
            "updated_by, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            _command_parameters(previous),
        )
    else:
        await connection.execute(
            f"UPDATE {table} SET name = ?, description = ?, content_json = ?, revision = ?, "
            "created_by = ?, updated_by = ?, created_at = ?, updated_at = ? "
            "WHERE guild_id = ? AND command_id = ? AND revision = ?",
            (
                previous.name,
                previous.description,
                _document_json(previous.document),
                previous.revision,
                previous.created_by,
                previous.updated_by,
                previous.created_at,
                previous.updated_at,
                previous.guild_id,
                previous.command_id,
                current.revision,
            ),
        )


async def _select_command(
    connection: Any, table: str, guild_id: int, name: str
) -> StoredCommand | None:
    cursor = await connection.execute(
        f"SELECT {_COMMAND_COLUMNS} FROM {table} WHERE guild_id = ? AND name = ?",
        (guild_id, name),
    )
    row = await cursor.fetchone()
    return _command_from_row(row) if row is not None else None


def _proposal_conflict(proposal: StoredProposal, command: StoredCommand | None) -> str | None:
    if proposal.operation == "create":
        return "command now exists" if command is not None else None
    if command is None:
        return "command no longer exists"
    if command.command_id != proposal.command_id or command.revision != proposal.expected_revision:
        return "command changed since this proposal was created"
    return None


async def _mark_proposal_with_connection(
    connection: Any,
    table: str,
    proposal_id: str,
    state: Literal["applied", "rejected"],
    decider_id: int,
    reason: str,
    now: float,
) -> None:
    cursor = await connection.execute(
        f"UPDATE {table} SET state = ?, decider_id = ?, decision_reason = ?, decided_at = ? "
        "WHERE proposal_id = ? AND state = 'pending'",
        (state, decider_id, reason, now, proposal_id),
    )
    if cursor.rowcount != 1:
        raise RevisionConflict("proposal")


__all__ = [
    "ApprovalResult",
    "CommandExists",
    "CommandMissing",
    "CommandStore",
    "ProposalCandidate",
    "RevisionConflict",
    "StoredProposal",
]
