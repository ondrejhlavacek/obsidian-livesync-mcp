import hashlib
import json
import logging
from collections.abc import Callable
from typing import Annotated, Literal
from uuid import UUID

import httpx
from mcp.server.fastmcp import FastMCP
from mcp.types import CallToolResult, TextContent, Tool
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
    model_validator,
)

from .client import ObsidianVaultClient
from .task_domain import DomainError, TaskDocument
from .task_storage import RECEIPT_FIELD, TaskError, TaskNote, TaskStorage

logger = logging.getLogger(__name__)

Priority = Literal["highest", "high", "medium", "low", "lowest"]
Revision = Annotated[str, StringConstraints(pattern=r"^[1-9][0-9]*-[0-9a-f]+$")]
SourceHash = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
Line = Annotated[int, Field(ge=0)]


class TaskInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TaskId(TaskInput):
    id: str


class LegacyTask(TaskInput):
    line: Line
    source_hash: SourceHash


class Occurrence(TaskInput):
    id: str | None
    line: Line
    source_hash: SourceHash
    section_id: str


class Create(TaskInput):
    type: Literal["create"]
    id: UUID
    section_id: str
    title: str
    due: str | None = None
    priority: Priority | None = None


class SetCompletion(TaskInput):
    type: Literal["set_completion"]
    completed: bool


class SetPriority(TaskInput):
    type: Literal["set_priority"]
    priority: Priority | None


class EditTitle(TaskInput):
    type: Literal["edit_title"]
    title: str


class EditNotes(TaskInput):
    type: Literal["edit_notes"]
    notes: str


class EditDue(TaskInput):
    type: Literal["edit_due"]
    due: str | None


class Move(TaskInput):
    type: Literal["move"]
    section_id: str


class Delete(TaskInput):
    type: Literal["delete"]
    descendant_count: Annotated[int, Field(ge=0)]


class AssignId(TaskInput):
    type: Literal["assign_id"]
    id: UUID


Operation = Annotated[
    Create
    | SetCompletion
    | SetPriority
    | EditTitle
    | EditNotes
    | EditDue
    | Move
    | Delete
    | AssignId,
    Field(discriminator="type"),
]


class SnapshotInput(TaskInput):
    pass


class ApplyCommand(TaskInput):
    command_id: UUID
    base_revision: Revision
    occurred_at: str
    action_timezone: str
    operation: Operation
    target: TaskId | LegacyTask | None = None
    expected: dict[str, str | bool | None] | None = None

    @model_validator(mode="after")
    def validate_target(self):
        if isinstance(self.operation, Create):
            if self.target is not None or self.expected is not None:
                raise ValueError("Create accepts the destination section on the operation.")
        elif self.target is None:
            raise ValueError("A task target is required.")
        if isinstance(self.operation, AssignId) and not isinstance(self.target, LegacyTask):
            raise ValueError("ID assignment requires a legacy task locator.")
        return self


class UndoCommand(TaskInput):
    command_id: UUID
    base_revision: Revision
    original_command_id: UUID
    expected_completed: Occurrence
    expected_successor: Occurrence | None = None


class Receipt(BaseModel):
    digest: SourceHash
    predecessor: Revision
    affected: list[Occurrence]
    undo: dict | None


TASK_TOOLS = {
    "get_task_snapshot": (SnapshotInput, "Read the configured task note's structured snapshot."),
    "apply_task_command": (ApplyCommand, "Apply one revision-guarded desired-state task command."),
    "undo_completion": (UndoCommand, "Conditionally undo a previous completion and its successor."),
}


def _result(body: dict, *, error: bool = False) -> CallToolResult:
    return CallToolResult(
        content=[TextContent(type="text", text=json.dumps(body, ensure_ascii=False))],
        structuredContent=body,
        isError=error,
    )


def _error(code: str, message: str, **details: object) -> CallToolResult:
    return _result(
        {"status": "error", "code": code, "message": message, "details": details}, error=True
    )


def _receipt_revision(note: TaskNote, receipt: Receipt) -> str:
    history = note.parent.get("_revisions", {})
    start = history.get("start", 0)
    ancestors = [f"{start - index}-{value}" for index, value in enumerate(history.get("ids", []))]
    if receipt.predecessor in ancestors[1:]:
        return ancestors[ancestors.index(receipt.predecessor) - 1]
    raise TaskError(
        "revision_conflict",
        "The recorded command can no longer be verified in this note history.",
        revision=note.revision,
        outcome="unknown",
    )


def _receipts(note: TaskNote) -> dict[str, Receipt]:
    raw = note.parent.get(RECEIPT_FIELD, {})
    if not isinstance(raw, dict):
        raise TaskError("replication_conflict", "The task note's command receipts are invalid.")
    try:
        return {command_id: Receipt.model_validate(receipt) for command_id, receipt in raw.items()}
    except ValidationError as exc:
        raise TaskError(
            "replication_conflict", "The task note's command receipts are invalid."
        ) from exc


def _affected_tasks(receipt: Receipt, tasks: list[dict]) -> list[dict]:
    affected = []
    for reference in receipt.affected:
        if reference.id is not None:
            candidates = [task for task in tasks if task["id"] == reference.id]
        else:
            candidates = [
                task
                for task in tasks
                if task["line"] == reference.line
                and task["source_hash"] == reference.source_hash
                and task["section_id"] == reference.section_id
            ]
        if len(candidates) == 1:
            affected.append(candidates[0])
    return affected


class TaskMCP(FastMCP):
    def __init__(self, path: str, client_factory: Callable[[], ObsidianVaultClient], **kwargs):
        self._path = path
        self._client_factory = client_factory
        super().__init__("obsidian-livesync-mcp", **kwargs)

    async def list_tools(self) -> list[Tool]:
        return [
            Tool(name=name, description=description, inputSchema=model.model_json_schema())
            for name, (model, description) in TASK_TOOLS.items()
        ]

    async def call_tool(self, name: str, arguments: dict) -> CallToolResult:
        if name not in TASK_TOOLS:
            return _error("forbidden", "This server exposes only the configured task note tools.")
        model, _ = TASK_TOOLS[name]
        try:
            command = model.model_validate_json(json.dumps(arguments), strict=True)
            storage = TaskStorage(self._client_factory(), self._path)
            note = await storage.read()
            document = TaskDocument(note.content, note.revision)
            if isinstance(command, SnapshotInput):
                return _result({"status": "ok", **document.snapshot(), "conflicts": []})
            return await self._mutate(command, storage, note, document)
        except ValidationError:
            return _error("invalid_command", "The arguments do not match the task tool schema.")
        except DomainError as exc:
            return _error(exc.code, exc.message)
        except TaskError as exc:
            return _error(exc.code, str(exc), **exc.details)
        except httpx.HTTPError:
            logger.warning("CouchDB request did not complete for task tool %s", name)
            return _error(
                "outcome_unknown",
                "CouchDB could not confirm the task tool request.",
                outcome="unknown",
            )

    async def _mutate(
        self,
        command: ApplyCommand | UndoCommand,
        storage: TaskStorage,
        note: TaskNote,
        document: TaskDocument,
    ) -> CallToolResult:
        args = command.model_dump(mode="json")
        digest = hashlib.sha256(
            json.dumps(args, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        command_id = str(command.command_id)
        receipts = _receipts(note)
        prior = receipts.get(command_id)
        if prior:
            if prior.digest != digest:
                raise TaskError("invalid_command", "This command ID was used with other arguments.")
            return _result(
                self._success(
                    "already_applied", command_id, _receipt_revision(note, prior), document, prior
                )
            )

        if command.base_revision != note.revision:
            if isinstance(command, ApplyCommand) and isinstance(command.target, LegacyTask):
                raise TaskError(
                    "ambiguous_identity",
                    "The note revision changed and the legacy task can no longer be identified.",
                    revision=note.revision,
                    outcome="unknown",
                )
            raise TaskError(
                "revision_conflict",
                "The note revision changed and no receipt proves the result of this command.",
                revision=note.revision,
                outcome="unknown",
            )

        if isinstance(command, UndoCommand):
            completion = receipts.get(str(command.original_command_id))
            if not completion or completion.undo is None:
                raise TaskError(
                    "revision_conflict",
                    "The originating completion cannot be verified. Refresh the task note.",
                    revision=note.revision,
                    outcome="unknown",
                )
            if _receipt_revision(note, completion) != note.revision:
                raise TaskError(
                    "revision_conflict",
                    "The task note changed after this completion. Review the current occurrences.",
                    revision=note.revision,
                )
            mutation = document.undo(
                command.base_revision,
                completion.undo,
                args["expected_completed"],
                args["expected_successor"],
            )
        else:
            mutation = document.apply(args)

        receipt = Receipt(
            digest=digest,
            predecessor=note.revision,
            affected=[
                Occurrence.model_validate({key: task[key] for key in Occurrence.model_fields})
                for task in mutation.affected_tasks
            ],
            undo=mutation.undo,
        )
        receipts[command_id] = receipt
        try:
            revision = await storage.publish(
                note,
                mutation.content,
                {key: value.model_dump(mode="json") for key, value in receipts.items()},
            )
        except TaskError as exc:
            if exc.code != "revision_conflict":
                raise
            fresh = await storage.read()
            recorded = _receipts(fresh).get(command_id)
            if recorded is None:
                exc.details["outcome"] = "unknown"
                raise
            if recorded.digest != digest:
                raise TaskError("invalid_command", "This command ID was used with other arguments.")
            return _result(
                self._success(
                    "already_applied",
                    command_id,
                    _receipt_revision(fresh, recorded),
                    TaskDocument(fresh.content, fresh.revision),
                    recorded,
                )
            )
        try:
            current = await storage.read()
            recorded = _receipts(current).get(command_id)
            if recorded != receipt or _receipt_revision(current, receipt) != revision:
                raise TaskError(
                    "revision_conflict",
                    "CouchDB accepted the command, but its result is no longer confirmed.",
                    revision=current.revision,
                    outcome="unknown",
                )
        except TaskError as exc:
            exc.details["accepted_revision"] = revision
            raise

        return _result(
            self._success(
                "applied",
                command_id,
                revision,
                TaskDocument(current.content, current.revision),
                receipt,
            )
        )

    @staticmethod
    def _success(
        status: str, command_id: str, revision: str, document: TaskDocument, receipt: Receipt
    ) -> dict:
        return {
            "status": status,
            "command_id": command_id,
            "accepted_revision": revision,
            "affected_tasks": _affected_tasks(receipt, document.snapshot()["tasks"]),
            "undo": (
                {key: receipt.undo[key] for key in ("completed", "successor")}
                if receipt.undo
                else None
            ),
        }
