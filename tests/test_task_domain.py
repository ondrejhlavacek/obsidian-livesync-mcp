from uuid import UUID, uuid5

import pytest

from obsidian_livesync_mcp.task_domain import EXPECTED, DomainError, TaskDocument

COMMAND = "f384b48e-54a8-49cb-94b5-670a31bfb50d"
TASK_ID = "dfa2bf0a-e773-4859-b1d6-dded60b6d717"
REVISION = "1-cafe"


def command(task, kind, *, revision=REVISION, **changes):
    target = {"id": task["id"]} if task["id"] else task["legacy"]
    return {
        "command_id": COMMAND,
        "base_revision": revision,
        "occurred_at": "2026-10-24T22:30:00Z",
        "action_timezone": "Europe/Prague",
        "target": target,
        "expected": {key: task[key] for key in EXPECTED[kind]},
        "operation": {"type": kind, **changes},
    }


def file(content, revision=REVISION):
    return TaskDocument(content, revision)


def tasks(document):
    return document.snapshot()["tasks"]


def assert_failure(code, operation, *args):
    with pytest.raises(DomainError) as failure:
        operation(*args)
    assert failure.value.code == code


def test_source_preserving_field_edits_with_mixed_eols_and_distractors():
    source = (
        "---\r\nexample: |\n  - [ ] YAML\r\n---\r\n"
        "# Inbox\r\n````tasks\n## Fake\r\n- [ ] Example\r\n```\n"
        "- [ ] Still example\r\n````\r\n"
        "<!--\n- [ ] Comment\n-->\n"
        "%%\n- [ ] Obsidian comment\n%%\n"
        "    - [ ] Indented code\n"
        "A [[wiki link]] and ordinary prose.\r\n"
        "- [ ] Task #tag (priority:: highest),  [due:: 2030-04-23]  ^link \r\n"
        "  Initial note.\n"
        "## Empty"
    )
    document = file(source)
    (task,) = tasks(document)
    assert task["title"] == "Task"
    assert task["notes"] == "Initial note."
    assert [section["name"] for section in document.snapshot()["sections"]] == ["Inbox", "Empty"]

    renamed = document.apply(command(task, "edit_title", title="Reviewed [[note]]"))
    assert renamed.content == source.replace("Task #tag", "Reviewed [[note]] #tag")
    rescheduled = document.apply(command(task, "edit_due", due="2030-05-23"))
    assert rescheduled.content == source.replace("2030-04-23", "2030-05-23")
    assert rescheduled.affected_tasks[0]["priority"] == "highest"
    unstarred = document.apply(command(task, "set_priority", priority=None))
    assert unstarred.content == source.replace(" (priority:: highest),", "")
    assert unstarred.affected_tasks[0]["priority"] is None


def test_nested_completion_move_delete_and_notes_round_trip():
    source = (
        "## Inbox\n"
        "- [ ] Parent  [id:: parent]\n"
        "  First note\n"
        "  - [ ] Child  [id:: child]\n"
        "    Child's notes\n"
        "\n"
        "  More parent notes\n"
        "  - [x] Sibling\n"
        "\n"
        "Ordinary prose.\n"
        "## Later\n"
        "- [ ] Another task"
    )
    document = file(source)
    parent, child, sibling, _ = tasks(document)
    assert child["parent_line"] == parent["line"]
    assert sibling["parent_line"] == parent["line"]
    assert parent["descendant_count"] == 2

    complete = document.apply(command(parent, "set_completion", completed=True))
    assert complete.content == source.replace(
        "- [ ] Parent  [id:: parent]", "- [x] Parent  [id:: parent]  [completion:: 2026-10-25]"
    )
    completed_document = file(complete.content, "2-cafe")
    assert [task["completed"] for task in tasks(completed_document)] == [True, False, True, False]
    undo = complete.undo
    assert completed_document.undo("2-cafe", undo, undo["completed"], None).content == source

    edited = document.apply(command(parent, "edit_notes", notes="New line\n\nNew paragraph"))
    assert "  - [ ] Child  [id:: child]\n    Child's notes\n\n  - [x] Sibling\n" in edited.content
    assert edited.affected_tasks[0]["notes"] == "New line\n\nNew paragraph"
    assert_failure(
        "invalid_command", document.apply, command(parent, "edit_notes", notes="- [ ] New child")
    )
    later = document.snapshot()["sections"][1]["id"]
    moved = document.apply(command(parent, "move", section_id=later))
    assert moved.content == (
        "## Inbox\n\nOrdinary prose.\n## Later\n"
        "- [ ] Parent  [id:: parent]\n"
        "  First note\n"
        "  - [ ] Child  [id:: child]\n"
        "    Child's notes\n"
        "\n"
        "  More parent notes\n"
        "  - [x] Sibling\n"
        "- [ ] Another task"
    )
    assert moved.affected_tasks[0]["descendant_count"] == 2
    assert_failure(
        "revision_conflict", document.apply, command(parent, "delete", descendant_count=1)
    )
    deleted = document.apply(command(parent, "delete", descendant_count=2))
    assert deleted.content == "## Inbox\n\nOrdinary prose.\n## Later\n- [ ] Another task"


def test_legacy_replay_and_explicit_assignment_require_same_snapshot_and_source_position():
    source = "## Inbox\n- [ ] Same title\n- [ ] Same title\n- [ ] Stable  [id:: stable]"
    document = file(source)
    first, second, _ = tasks(document)
    replay = command(second, "set_completion", completed=True)
    assert document.apply(replay).content == source.replace(
        "- [ ] Same title\n- [ ] Stable",
        "- [x] Same title  [completion:: 2026-10-25]\n- [ ] Stable",
    )
    assert_failure("ambiguous_identity", file(source, "2-cafe").apply, replay)
    replay["target"]["source_hash"] = "wrong"
    assert_failure("ambiguous_identity", document.apply, replay)

    assigned = document.apply(command(first, "assign_id", id=TASK_ID))
    assert assigned.content == source.replace(
        "- [ ] Same title", f"- [ ] Same title  [id:: {TASK_ID}]", 1
    )
    new_document = file(assigned.content)
    assert_failure(
        "ambiguous_identity", new_document.apply, command(second, "assign_id", id=TASK_ID)
    )

    duplicate = file(source + "\n- [ ] Duplicate  [id:: stable]")
    assert_failure(
        "ambiguous_identity",
        duplicate.apply,
        command(tasks(duplicate)[-1], "edit_title", title="Anything"),
    )


def test_concurrent_independent_fields_and_changed_relevant_state():
    source = "## Inbox\n- [ ] Task  [id:: stable]  [priority:: high]"
    old_document = file(source)
    (task,) = tasks(old_document)
    current = file(source.replace("Task", "Renamed"), "2-cafe")
    assert current.apply(command(task, "set_priority", priority="highest")).content.endswith(
        "Renamed  [id:: stable]  [priority:: highest]"
    )
    assert_failure("revision_conflict", current.apply, command(task, "edit_title", title="Other"))


def test_monthly_successor_suffix_and_undo_with_original_prague_action_day():
    source = (
        "## Bills\r\n"
        "- [ ] Pay [link](https://example.invalid)  [id:: old]"
        " (dependsOn:: prerequisite), [priority:: highest]  [due:: 2030-12-23]"
        " [repeat:: every month on the 23rd]  ^stay"
    )
    document = file(source)
    (task,) = tasks(document)
    mutation = document.apply(command(task, "set_completion", completed=True))
    completed, successor = mutation.affected_tasks
    successor_id = str(uuid5(UUID(COMMAND), "recurrence-successor"))
    assert successor["id"] == successor_id
    assert successor["due"] == "2031-01-23"
    assert completed["completion"] == "2026-10-25"
    assert mutation.content == (
        "## Bills\r\n"
        "- [ ] Pay [link](https://example.invalid)"
        f"  [id:: {successor_id}]  [priority:: highest]"
        "  [repeat:: every month on the 23rd]  [due:: 2031-01-23]\r\n"
        + source.split("\r\n")[1]
        .replace("- [ ]", "- [x]")
        .replace("  ^stay", "  [completion:: 2026-10-25]  ^stay")
    )
    receipt = mutation.undo
    followup = file(mutation.content, "2-cafe")
    undo = followup.undo("2-cafe", receipt, receipt["completed"], receipt["successor"])
    assert undo.content == source
    updated = file(mutation.content.replace("2031-01-23", "2031-02-23"), "3-cafe")
    assert_failure(
        "revision_conflict",
        updated.undo,
        "3-cafe",
        receipt,
        receipt["completed"],
        receipt["successor"],
    )
    reopened = followup.apply(
        command(completed, "set_completion", completed=False, revision="2-cafe")
    )
    assert [task["id"] for task in tasks(file(reopened.content))] == [successor_id, "old"]


def test_unsupported_recurrence_preserves_rule_and_dates_during_safe_edits():
    source = (
        "## Bills\n- [ ] Pay  [id:: item]  [priority:: low]  [repeat:: every week when done]"
        " (scheduled:: 2030-01-22), [due:: 2030-01-23]"
    )
    document = file(source)
    (task,) = tasks(document)
    assert not task["recurrence_supported"]
    assert task["issues"]
    for kind, change in (
        ("set_completion", {"completed": True}),
        ("edit_due", {"due": "2030-02-23"}),
        ("delete", {"descendant_count": 0}),
    ):
        assert_failure("unsupported_rule", document.apply, command(task, kind, **change))
    assert document.apply(command(task, "edit_title", title="Reviewed")).content == source.replace(
        "Pay", "Reviewed"
    )
    assert document.apply(command(task, "set_priority", priority="high")).content == source.replace(
        "priority:: low", "priority:: high"
    )
    assert document.apply(command(task, "edit_notes", notes="Payment notes")).content == (
        source + "\n  Payment notes\n"
    )


def test_invalid_times_dates_metadata_and_ambiguous_destinations_are_visible():
    source = (
        "## Inbox\n"
        "- [ ] Valid  [id:: stable]\n"
        "- [ ] Invalid  [due:: 2026-02-30]\n"
        "- [ ] Invalid identity (id:: valid) [id:: other]\n"
        "## Inbox\n"
    )
    document = file(source)
    valid, invalid_date, conflicting_id = tasks(document)
    assert invalid_date["issues"]
    assert conflicting_id["issues"]
    stale_time = command(valid, "set_completion", completed=True)
    stale_time["occurred_at"] = "2026-10-24T22:30:00"
    assert_failure("invalid_command", document.apply, stale_time)
    assert_failure("invalid_command", document.apply, command(valid, "edit_due", due="2026-2-3"))
    create = command(valid, "assign_id", id=TASK_ID)
    create.pop("target")
    create["operation"] = {
        "type": "create",
        "id": TASK_ID,
        "title": "New task",
        "section_id": valid["section_id"],
    }
    assert_failure("ambiguous_identity", document.apply, create)
    create["operation"]["section_id"] = "root"
    assert file("").apply(create).content == f"- [ ] New task  [id:: {TASK_ID}]"


def test_cascade_keeps_ambiguous_and_unsupported_descendants_protected():
    source = (
        "## Inbox\n"
        "- [ ] Parent  [id:: parent]\n"
        "  - [ ] Duplicate child  [id:: duplicate]\n"
        "- [ ] Other task  [id:: duplicate]\n"
        "## Later"
    )
    doc = file(source)
    parent, *_ = tasks(doc)
    later = doc.snapshot()["sections"][1]["id"]
    assert_failure("ambiguous_identity", doc.apply, command(parent, "move", section_id=later))
    assert_failure("ambiguous_identity", doc.apply, command(parent, "delete", descendant_count=1))
    assert doc.apply(command(parent, "set_completion", completed=True)).affected_tasks[0][
        "completed"
    ]

    source = source.replace(
        "Duplicate child  [id:: duplicate]",
        "Recurring child  [id:: recurring]  [repeat:: every week]",
    )
    doc = file(source)
    parent, *_ = tasks(doc)
    assert_failure("unsupported_rule", doc.apply, command(parent, "delete", descendant_count=1))
    assert (
        doc.apply(command(parent, "move", section_id=later)).affected_tasks[0]["descendant_count"]
        == 1
    )


@pytest.mark.parametrize(
    ("parent", "child", "sibling"),
    [
        ("- [ ] Parent", "  - [ ] Child", " - [ ] Sibling"),
        ("10. [ ] Parent", "    - [ ] Child", "   - [ ] Sibling"),
        ("-\t[ ] Parent", "\t- [ ] Child", "   - [ ] Sibling"),
    ],
)
def test_structural_edits_preserve_commonmark_siblings(parent, child, sibling):
    source = f"## Inbox\n{parent}\n{child}\n{sibling}\n## Later\n"
    document = file(source)
    top, nested, peer = tasks(document)
    assert top["descendant_count"] == 1
    assert nested["parent_line"] == top["line"]
    assert peer["parent_line"] is None
    assert document.apply(command(top, "delete", descendant_count=1)).content == (
        f"## Inbox\n{sibling}\n## Later\n"
    )
    later = document.snapshot()["sections"][1]["id"]
    assert document.apply(command(top, "move", section_id=later)).content == (
        f"## Inbox\n{sibling}\n## Later\n{parent}\n{child}\n"
    )
    assert document.apply(command(top, "edit_notes", notes="A note")).content.endswith(
        f"{child}\n{sibling}\n## Later\n"
    )


def test_ambiguous_lazy_list_continuation_blocks_structural_edits():
    document = file("## Inbox\n- [ ] Parent\nLazy paragraph\n  - [ ] Follow-on child\n## Later")
    parent, child = tasks(document)
    assert child["parent_line"] == parent["line"]
    assert parent["descendant_count"] == 1
    assert_failure("invalid_command", document.apply, command(parent, "delete", descendant_count=1))
    assert_failure(
        "invalid_command", document.apply, command(parent, "edit_notes", notes="New notes")
    )


@pytest.mark.parametrize(
    "header",
    [
        "- [ ] Old title #todo",
        "- [ ] Old title #todo  [id:: example]",
        "- [ ] Old title  [id:: example] #todo",
        "- [ ] Old title  [id:: example] #todo  [priority:: highest]  ^block",
        "- [ ] #todo Old title  [id:: example]",
    ],
)
def test_rename_preserves_global_filter_tag_bytes(header):
    document = file("## Inbox\n" + header)
    (task,) = tasks(document)
    assert task["title"] == "Old title"
    renamed = document.apply(command(task, "edit_title", title="New title"))
    assert renamed.content == document.content.replace("Old title", "New title")


def test_occurrence_changes_track_ancestry_and_cascades_but_ignore_line_shifts():
    source = (
        "- [ ] Parent  [id:: parent]\n  - [x] Child  [id:: child]\n- [ ] Other task  [id:: other]"
    )
    original = file(source)
    assert original.changed_occurrence_ids(file("\n" + source)) == []
    assert original.changed_occurrence_ids(file(source.replace("Parent", "Renamed"))) == [
        "child",
        "parent",
    ]
    assert original.changed_occurrence_ids(file(source.replace("Other task", "Renamed"))) == [
        "other",
    ]
    assert original.changed_occurrence_ids(file(source.splitlines()[-1])) == ["child", "parent"]


@pytest.mark.parametrize(
    "residual",
    [
        "Lazy continuation\n- [ ] Following [id:: following]",
        "Lazy continuation\n  Indented tail\n- [ ] Following [id:: following]",
        "First lazy line\nSecond lazy line\n- [ ] Following [id:: following]",
        "Terminal lazy line",
    ],
)
def test_ambiguous_list_with_no_descendants_preserves_residual_prose(residual):
    document = file(
        "## Later\n"
        "## Inbox\n"
        "- [ ] Previous [id:: previous]\n"
        "- [ ] Target [id:: target]\n"
        "  Opening note under target\n" + residual
    )
    target = tasks(document)[1]
    later = document.snapshot()["sections"][0]["id"]
    assert target["descendant_count"] == 0
    for kind, changes in [
        ("delete", {"descendant_count": 0}),
        ("move", {"section_id": later}),
        ("edit_notes", {"notes": "Replacement notes"}),
    ]:
        assert not target["capabilities"][kind]
        assert_failure("invalid_command", document.apply, command(target, kind, **changes))
    renamed = document.apply(command(target, "edit_title", title="Renamed"))
    assert renamed.content == document.content.replace("Target", "Renamed")


def test_rename_requires_preserving_exact_inline_filter_tag():
    source = "- [ ] Pay #todo electricity  [id:: bills]"
    document = file(source)
    (task,) = tasks(document)
    for title in ("Pay electricity invoice", "Pay #todo-later electricity invoice"):
        assert_failure("invalid_command", document.apply, command(task, "edit_title", title=title))
    rename = document.apply(command(task, "edit_title", title="Pay #todo electricity invoice"))
    assert rename.content == "- [ ] Pay #todo electricity invoice  [id:: bills]"
