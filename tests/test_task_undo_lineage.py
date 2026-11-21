import pytest

from obsidian_livesync_mcp.task_storage import TaskError, TaskNote
from obsidian_livesync_mcp.task_tools import Occurrence, Receipt, _verify_undo_lineage


def test_stable_undo_requires_proven_unmodified_occurrences_through_all_revisions():
    note = TaskNote(
        {"_rev": "4-dd", "_revisions": {"start": 4, "ids": ["dd", "cc", "bb", "aa"]}},
        "",
    )
    source_hash = "a" * 64
    completed = Occurrence(id="original", line=1, section_id="root", source_hash=source_hash)
    successor = Occurrence(id="successor", line=0, section_id="root", source_hash=source_hash)
    completion = Receipt(
        digest=source_hash,
        predecessor="1-aa",
        affected=[completed, successor],
        undo={},
        changed_ids=["original", "successor"],
    )
    unrelated = Receipt(
        digest=source_hash,
        predecessor="2-bb",
        affected=[],
        undo=None,
        changed_ids=["unrelated"],
    )
    unrelated_again = unrelated.model_copy(update={"predecessor": "3-cc"})
    receipts = {"done": completion, "edit1": unrelated, "edit2": unrelated_again}
    _verify_undo_lineage(note, completion, receipts)

    for receipts_without_proof in (
        {"done": completion, "edit2": unrelated_again},
        {**receipts, "edit1": unrelated.model_copy(update={"changed_ids": None})},
        {**receipts, "edit1": unrelated.model_copy(update={"changed_ids": ["successor"]})},
        {**receipts, "edit2": unrelated_again.model_copy(update={"changed_ids": ["original"]})},
    ):
        with pytest.raises(TaskError, match="history could not be verified"):
            _verify_undo_lineage(note, completion, receipts_without_proof)

    legacy = completion.model_copy(
        update={"affected": [completed.model_copy(update={"id": None}), successor]},
    )
    with pytest.raises(TaskError, match="history could not be verified"):
        _verify_undo_lineage(note, legacy, {**receipts, "done": legacy})
