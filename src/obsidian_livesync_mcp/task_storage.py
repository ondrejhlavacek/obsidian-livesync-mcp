import asyncio
import time
from dataclasses import dataclass
from typing import Literal

from .chunking import split_chunks
from .client import CHUNK_DATA_BATCH_SIZE, ObsidianVaultClient
from .utils import encode_doc_id, generate_chunk_id

TaskErrorCode = Literal[
    "revision_conflict",
    "replication_conflict",
    "not_found",
    "ambiguous_identity",
    "unsupported_rule",
    "invalid_command",
    "forbidden",
    "outcome_unknown",
]

RECEIPT_FIELD = "task_command_receipts"


class TaskError(Exception):
    def __init__(self, code: TaskErrorCode, message: str, **details: object):
        super().__init__(message)
        self.code = code
        self.details = details


@dataclass(frozen=True)
class TaskNote:
    parent: dict
    content: str

    @property
    def revision(self) -> str:
        return self.parent["_rev"]


class TaskStorage:
    def __init__(self, client: ObsidianVaultClient, path: str):
        self.client = client
        self.path = path

    async def _parent(self) -> dict:
        doc = await self.client._get_doc(self.path, params={"conflicts": "true", "revs": "true"})
        if not doc or doc.get("path") != self.path:
            raise TaskError("not_found", "The configured task note was not found.")
        if doc.get("_conflicts"):
            raise TaskError(
                "replication_conflict",
                "Resolve the task note's sibling revisions in Obsidian before retrying.",
                revision=doc["_rev"],
                conflicts=doc["_conflicts"],
            )
        if doc.get("deleted"):
            raise TaskError("not_found", "The configured task note was deleted.")
        if doc.get("type") != "plain" or not isinstance(doc.get("children"), list):
            raise TaskError(
                "replication_conflict", "The task note does not use supported LiveSync storage."
            )
        return doc

    async def _read_chunks(self, children: list[str]) -> str:
        if any(not isinstance(child, str) or not child.startswith("h:") for child in children):
            raise TaskError(
                "replication_conflict", "The task note references unsupported LiveSync chunks."
            )
        client = await self.client._get_client()

        async def fetch(chunk_id: str) -> tuple[str, str]:
            response = await client.get(f"/{encode_doc_id(chunk_id)}", params={"conflicts": "true"})
            if response.status_code == 404:
                raise TaskError(
                    "replication_conflict", "The task note's chunks have not finished replicating."
                )
            response.raise_for_status()
            chunk = response.json()
            data = chunk.get("data")
            if (
                chunk.get("_conflicts")
                or chunk.get("type") != "leaf"
                or not isinstance(data, str)
                or generate_chunk_id(data) != chunk_id
            ):
                raise TaskError(
                    "replication_conflict",
                    "The task note references divergent or unsupported LiveSync chunks.",
                )
            return chunk_id, data

        unique_ids = list(dict.fromkeys(children))
        data = {}
        for start in range(0, len(unique_ids), CHUNK_DATA_BATCH_SIZE):
            data.update(
                await asyncio.gather(
                    *(
                        fetch(chunk_id)
                        for chunk_id in unique_ids[start : start + CHUNK_DATA_BATCH_SIZE]
                    )
                )
            )
        return "".join(data[chunk_id] for chunk_id in children)

    async def read(self) -> TaskNote:
        parent = await self._parent()
        content = await self._read_chunks(parent["children"])
        current = await self._parent()
        if current["_rev"] != parent["_rev"]:
            raise TaskError(
                "revision_conflict",
                "The task note changed while reading it.",
                revision=current["_rev"],
            )
        return TaskNote(current, content)

    async def publish(self, note: TaskNote, content: str, receipts: dict) -> str:
        raw = content.encode("utf-8")
        children = []
        for data in split_chunks(raw, is_text=True):
            chunk_id = generate_chunk_id(data)
            try:
                await self.client._put_chunk_doc(chunk_id, data)
            except ValueError as exc:
                raise TaskError(
                    "replication_conflict", "The new task note chunks could not be verified."
                ) from exc
            children.append(chunk_id)

        if await self._read_chunks(children) != content:
            raise TaskError("replication_conflict", "The new task note chunks do not match.")
        current = await self._parent()
        if current["_rev"] != note.revision:
            raise TaskError(
                "revision_conflict",
                "The task note changed before publication.",
                revision=current["_rev"],
            )

        parent = {
            key: value for key, value in current.items() if key not in {"_conflicts", "_revisions"}
        }
        parent.update(
            children=children,
            mtime=int(time.time() * 1000),
            size=len(raw),
        )
        parent[RECEIPT_FIELD] = receipts

        client = await self.client._get_client()
        response = await client.put(f"/{encode_doc_id(parent['_id'])}", json=parent)
        if response.status_code == 409:
            fresh = await self._parent()
            raise TaskError(
                "revision_conflict",
                "The task note changed before publication.",
                revision=fresh["_rev"],
            )
        response.raise_for_status()
        revision = response.json().get("rev")
        if not isinstance(revision, str):
            raise TaskError(
                "outcome_unknown", "CouchDB did not return the result of the task publication."
            )
        return revision
