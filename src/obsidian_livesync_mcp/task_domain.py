from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime
from uuid import UUID, uuid5
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from markdown_it import MarkdownIt

FIELD_ORDER = (
    "id",
    "dependsOn",
    "priority",
    "repeat",
    "onCompletion",
    "created",
    "start",
    "scheduled",
    "due",
    "cancelled",
    "completion",
)
DATE_FIELDS = {"created", "start", "scheduled", "due", "cancelled", "completion"}
PRIORITIES = {"highest", "high", "medium", "low", "lowest"}
MONTHLY_RULE = "every month on the 23rd"
RECURRENCE_MESSAGE = "Complete or edit this recurring task in Obsidian."
FIELD_NAMES = "|".join(FIELD_ORDER)
FIELD = re.compile(
    rf"(?P<space>[ \t]*)(?:(?P<open>\[)[ \t]*(?P<square>{FIELD_NAMES})::"
    rf"[ \t]*(?P<bracket_value>[^\[\]]*?)[ \t]*\]|"
    rf"\([ \t]*(?P<round>{FIELD_NAMES})::[ \t]*(?P<paren_value>[^()]*?)[ \t]*\))"
    r"(?:[ \t]*,)?$"
)
FIELD_LIKE = re.compile(rf"[\[(][ \t]*(?:{FIELD_NAMES})[ \t]*::", re.IGNORECASE)
TASK_ID = re.compile(r"[a-zA-Z0-9_-]+")
ID_CANDIDATE = re.compile(
    r"\[[ \t]*id::[ \t]*([a-zA-Z0-9_-]+)[ \t]*\]|\([ \t]*id::[ \t]*([a-zA-Z0-9_-]+)[ \t]*\)"
)
TASK = re.compile(
    r"^(?P<indent>[ \t]*)(?P<quote>(?:>[ \t]*)*)"
    r"(?P<marker>[-*+]|[0-9]+[.)])[ \t]+\[(?P<status>[^\n])\](?P<body>.*)$"
)
HEADING = re.compile(r"^ {0,3}(?P<marker>#{1,6})(?:[ \t]+|$)(?P<title>.*)$")
MARKDOWN = MarkdownIt("commonmark")
BLOCK_LINK = re.compile(r"[ \t]+\^[a-zA-Z0-9-]+$")
TAG_TEXT = r"#[^ !@#$%^&*(),.?\x22:|<>\[\]()\t`]+"
TRAILING_TAG = re.compile(rf"[ \t]+{TAG_TEXT}$")
LEADING_TAGS = re.compile(rf"(?:[ \t]+{TAG_TEXT})+[ \t]+")
INLINE_TAG = re.compile(TAG_TEXT)
LINE_BREAK = re.compile(r"\r\n|\n|\r")
END_LINE = re.compile(r"(?:\r\n|\n|\r)$")
ID_VALUE = {
    "id": TASK_ID,
    "dependsOn": re.compile(r"[a-zA-Z0-9_-]+(?:[ \t]*,[ \t]*[a-zA-Z0-9_-]+)*"),
}
EXPECTED = {
    "set_completion": ("completed", "completion", "recurrence", "due"),
    "set_priority": ("priority",),
    "edit_title": ("title",),
    "edit_notes": ("notes",),
    "edit_due": ("due",),
    "move": ("section_id", "source_hash"),
    "delete": ("source_hash",),
    "assign_id": (),
}


class DomainError(ValueError):
    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(message)


@dataclass
class Mutation:
    content: str
    affected_tasks: list[dict]
    undo: dict | None = None


@dataclass
class Field:
    value: str
    start: int
    end: int
    value_start: int
    value_end: int


@dataclass
class Section:
    id: str
    name: str | None
    line: int | None
    start: int
    level: int
    ambiguous: bool = False

    def snapshot(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "line": self.line,
            "level": self.level,
            "ambiguous": self.ambiguous,
        }


def digest(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def without_eol(line: str) -> str:
    return END_LINE.sub("", line)


def eol(line: str) -> str:
    return line[len(without_eol(line)) :]


def indentation(line: str) -> int:
    return len(line[: len(line) - len(line.lstrip(" \t"))].expandtabs(4))


def deindent(line: str, width: int) -> str:
    if width == 0 or not line.strip():
        return line
    end = 0
    column = 0
    while end < len(line) and line[end] in " \t" and column < width:
        column += (4 - column % 4) if line[end] == "\t" else 1
        end += 1
    return " " * max(0, column - width) + line[end:]


def parse_date(value: object) -> date:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
        raise ValueError("Expected a calendar date in YYYY-MM-DD format.")
    return date.fromisoformat(value)


def optional_date(value: object) -> str | None:
    if value is None:
        return None
    try:
        return parse_date(value).isoformat()
    except ValueError as error:
        raise DomainError("invalid_command", "Use a valid YYYY-MM-DD calendar date.") from error


def canonical_uuid(value: str) -> str:
    try:
        parsed = UUID(value)
    except (AttributeError, TypeError, ValueError) as error:
        raise DomainError("invalid_command", "Use a canonical UUID.") from error
    if str(parsed) != value:
        raise DomainError("invalid_command", "Use a canonical lowercase UUID.")
    return str(parsed)


def action_day(occurred_at: str, action_timezone: str) -> date:
    try:
        instant = datetime.fromisoformat(
            occurred_at.removesuffix("Z") + ("+00:00" if occurred_at.endswith("Z") else "")
        )
        if "T" not in occurred_at or instant.utcoffset() is None:
            raise ValueError("Expected a timezone-aware instant.")
        return instant.astimezone(ZoneInfo(action_timezone)).date()
    except (AttributeError, TypeError, ValueError, OverflowError, ZoneInfoNotFoundError) as error:
        raise DomainError(
            "invalid_command", "Supply an aware ISO timestamp and IANA timezone."
        ) from error


def check_title(value: str) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or (
            LINE_BREAK.search(value)
            or FIELD_LIKE.search(value)
            or BLOCK_LINK.search(value.rstrip())
        )
    ):
        raise DomainError("invalid_command", "Enter a single-line title without Tasks metadata.")
    return value.strip()


def check_priority(value: str | None) -> str | None:
    if value is not None and value not in PRIORITIES:
        raise DomainError("invalid_command", "Choose a supported priority or clear the field.")
    return value


@dataclass
class Task:
    line: int
    section: Section
    header: str
    status: str
    status_start: int
    title: str
    title_start: int
    title_end: int
    suffix_end: int
    indent: int
    note_indent: int
    fields: dict[str, Field]
    issues: list[str]
    parent: Task | None
    end: int
    depth: int = 0
    descendant_count: int = 0
    ambiguous_descendant: bool = False
    unsupported_descendant: bool = False
    uncertain_block: bool = False
    note_lines: list[int] = field(default_factory=list)
    block: str = ""
    notes: str = ""

    def get(self, key: str) -> str | None:
        return self.fields[key].value if key in self.fields else None

    @property
    def id(self) -> str | None:
        value = self.get("id")
        return value if value and TASK_ID.fullmatch(value) else None

    @property
    def completed(self) -> bool:
        return self.status in {"x", "X"}

    @property
    def recurrence_supported(self) -> bool:
        due = self.get("due")
        return (
            self.get("repeat") == MONTHLY_RULE
            and due is not None
            and due.endswith("-23")
            and due < "9999-12-23"
            and not any(name in self.fields for name in ("start", "scheduled", "onCompletion"))
            and self.end == self.line + 1
            and not self.uncertain_block
            and not self.issues
        )

    @property
    def unsupported_recurrence(self) -> bool:
        return "repeat" in self.fields and not self.recurrence_supported

    def id_candidates(self) -> set[str]:
        return {square or round_ for square, round_ in ID_CANDIDATE.findall(self.header)}

    def capabilities(self) -> dict[str, bool]:
        editable = not self.issues
        recurring_edit = editable and not self.unsupported_recurrence
        return {
            "set_completion": recurring_edit and "onCompletion" not in self.fields,
            "set_priority": editable,
            "edit_title": editable,
            "edit_notes": editable and not self.uncertain_block,
            "edit_due": recurring_edit,
            "move": editable and not (self.ambiguous_descendant or self.uncertain_block),
            "delete": recurring_edit
            and not (
                self.ambiguous_descendant or self.unsupported_descendant or self.uncertain_block
            ),
            "assign_id": editable and self.id is None,
        }

    def snapshot(self) -> dict:
        issues = list(self.issues)
        if self.unsupported_recurrence:
            issues.append(RECURRENCE_MESSAGE)
        if self.uncertain_block:
            issues.append("Edit this task's ambiguous Markdown list block in Obsidian.")
        if self.ambiguous_descendant:
            issues.append(
                "Resolve duplicate descendant Tasks IDs before moving or deleting this task."
            )
        if self.unsupported_descendant:
            issues.append("Delete this task in Obsidian to remove its recurring descendants.")
        if "onCompletion" in self.fields:
            issues.append("Handle this configured completion action in Obsidian.")
        source_hash = digest(self.block)
        return {
            "id": self.id,
            "line": self.line,
            "source_hash": source_hash,
            "legacy": {"line": self.line, "source_hash": source_hash} if self.id is None else None,
            "section_id": self.section.id,
            "parent_line": self.parent.line if self.parent else None,
            "depth": self.depth,
            "indent": self.indent,
            "title": self.title,
            "notes": self.notes,
            "priority": self.get("priority"),
            "due": self.get("due"),
            "completed": self.completed,
            "completion": self.get("completion"),
            "recurrence": self.get("repeat"),
            "recurrence_supported": self.recurrence_supported,
            "descendant_count": self.descendant_count,
            "capabilities": self.capabilities(),
            "issues": issues,
        }

    def edit_field(self, key: str, value: str | None, header: str | None = None) -> str:
        source = self.header if header is None else header
        existing = self.fields.get(key)
        if existing:
            if value is None:
                return source[: existing.start] + source[existing.end :]
            return source[: existing.value_start] + value + source[existing.value_end :]
        if value is None:
            return source
        rank = FIELD_ORDER.index(key)
        position = min(
            (other.start for name, other in self.fields.items() if FIELD_ORDER.index(name) > rank),
            default=self.suffix_end,
        )
        return source[:position] + f"  [{key}:: {value}]" + source[position:]


def parse_suffix(header: str, body_start: int) -> tuple[int, int, dict[str, Field], list[str]]:
    fields = {}
    issues = []
    tail = len(header.rstrip(" \t"))
    link = BLOCK_LINK.search(header, body_start, tail)
    if link:
        tail = link.start()
    suffix_end = tail
    while tail > body_start:
        match = FIELD.search(header, body_start, tail)
        if match is None:
            tag = TRAILING_TAG.search(header, body_start, tail)
            if tag:
                tail = tag.start()
                continue
            break
        key = match.group("square") or match.group("round")
        group = "bracket_value" if match.group("square") else "paren_value"
        value = match.group(group)
        if key in fields:
            issues.append(f"Repeated Tasks field: {key}.")
        fields[key] = Field(value, match.start(), match.end(), *match.span(group))
        tail = match.start()
    if FIELD_LIKE.search(header, body_start, tail):
        issues.append("Malformed or misplaced Tasks metadata.")
    for key, field_value in fields.items():
        if key in DATE_FIELDS:
            try:
                parse_date(field_value.value)
            except ValueError:
                issues.append(f"Invalid Tasks date: {key}.")
        elif key in ID_VALUE and not ID_VALUE[key].fullmatch(field_value.value):
            issues.append(f"Invalid Tasks field: {key}.")
        elif key == "priority" and field_value.value not in PRIORITIES:
            issues.append("Invalid Tasks priority.")
    return tail, suffix_end, fields, issues


def read_task(
    line: int,
    source: str,
    matched: re.Match,
    section: Section,
    parent: Task | None,
) -> Task:
    raw = without_eol(source)
    body_start = matched.start("body")
    tail, suffix_end, fields, issues = parse_suffix(raw, body_start)
    title = raw[body_start:tail]
    leading_tags = LEADING_TAGS.match(title)
    if leading_tags:
        body_start += leading_tags.end()
        title = title[leading_tags.end() :]
    if matched.group("status") not in {" ", "x", "X"} or matched.group("quote"):
        issues.append("This Markdown checkbox form is read-only.")
    if not title.strip():
        issues.append("This task is missing a title.")
    return Task(
        line,
        section,
        raw,
        matched.group("status"),
        matched.start("status"),
        title.strip(),
        body_start + len(title) - len(title.lstrip(" \t")),
        body_start + len(title.rstrip(" \t")),
        suffix_end,
        indentation(raw),
        len(raw[: matched.start("status") - 1].expandtabs(4)),
        fields,
        issues,
        parent,
        line + 1,
        depth=parent.depth + 1 if parent else 0,
    )


def frontmatter_end(lines: list[str]) -> int:
    if not lines or without_eol(lines[0]).removeprefix("\ufeff") != "---":
        return 0
    return next(
        (
            index + 1
            for index, line in enumerate(lines[1:], 1)
            if without_eol(line) in {"---", "..."}
        ),
        len(lines),
    )


def source_context(task: Task) -> list[str]:
    context = []
    while task.parent:
        task = task.parent
        context.append(digest(task.header))
    return context


def undo_guard(task: Task) -> dict:
    state = task.snapshot()
    return {key: state[key] for key in ("id", "line", "source_hash", "section_id")}


class TaskDocument:
    def __init__(self, content: str, revision: str):
        self.content = content
        self.revision = revision
        self.content_hash = digest(content)
        offsets = [0, *(match.end() for match in LINE_BREAK.finditer(content))]
        if offsets[-1] != len(content):
            offsets.append(len(content))
        self.lines = [content[start:end] for start, end in zip(offsets, offsets[1:])]
        self.newline = next((eol(line) for line in self.lines if eol(line)), "\n")
        self.start = frontmatter_end(self.lines)
        self.sections = [Section("root", None, None, self.start, 0)]
        self.tasks: list[Task] = []
        self._parse()
        self.by_line = {task.line: task for task in self.tasks}
        self.by_id: dict[str, list[Task]] = {}
        for task in self.tasks:
            for candidate in task.id_candidates():
                self.by_id.setdefault(candidate, []).append(task)
        for matches in self.by_id.values():
            if len(matches) > 1:
                for task in matches:
                    task.issues.append("Duplicate Tasks ID.")
        for task in reversed(self.tasks):
            if task.parent:
                task.parent.descendant_count += 1 + task.descendant_count
                task.parent.ambiguous_descendant |= (
                    task.ambiguous_descendant or "Duplicate Tasks ID." in task.issues
                )
                task.parent.uncertain_block |= task.uncertain_block
                task.parent.unsupported_descendant |= (
                    task.unsupported_descendant or task.unsupported_recurrence
                )
        counts = Counter(section.id for section in self.sections)
        for section in self.sections:
            section.ambiguous = counts[section.id] > 1

    def _source_tokens(self):
        source = "".join(self.lines[self.start :])
        tokens = MARKDOWN.parse(source)
        if "%%" not in source:
            return tokens
        literal_lines = {
            line
            for token in tokens
            if token.type in {"fence", "code_block", "html_block"} and token.map
            for line in range(*token.map)
        }
        masked = self.lines[self.start :]
        comment = False
        for index, line in enumerate(masked):
            body = line.lstrip(" \t")
            if comment:
                masked[index] = eol(line)
                comment = "%%" not in body
            elif body.startswith("%%") and index not in literal_lines:
                masked[index] = eol(line)
                comment = "%%" not in body[2:]
        return MARKDOWN.parse("".join(masked))

    def _parse(self):
        items: dict[int, tuple[int, int | None]] = {}
        heading_lines = set()
        item_stack: list[int] = []
        for token in self._source_tokens():
            if token.type == "list_item_open" and token.map:
                start, end = (position + self.start for position in token.map)
                items.setdefault(start, (end, item_stack[-1] if item_stack else None))
                item_stack.append(start)
            elif token.type == "list_item_close":
                item_stack.pop()
            elif token.type == "heading_open" and token.level == 0 and token.map:
                heading_lines.add(token.map[0] + self.start)

        active: list[Task] = []
        parsed: dict[int, Task] = {}
        headings: list[tuple[int, str]] = []
        section = self.sections[0]
        for index in range(self.start, len(self.lines)):
            raw = without_eol(self.lines[index])
            if not raw.strip():
                continue
            indent = indentation(raw)
            while active and (
                index >= items[active[-1].line][0] or indent < active[-1].note_indent
            ):
                task = active.pop()
                task.uncertain_block |= index < items[task.line][0]
            for owner in active:
                owner.end = index + 1

            heading = HEADING.match(raw) if index in heading_lines else None
            if heading:
                level = len(heading["marker"])
                title = re.sub(r"[ \t]+#+[ \t]*$", "", heading["title"]).strip()
                while headings and headings[-1][0] >= level:
                    headings.pop()
                headings.append((level, title))
                key = digest(json.dumps(headings, ensure_ascii=False))
                section = Section(key, title, index, index + 1, level)
                self.sections.append(section)
                continue

            matched = TASK.match(raw) if index in items else None
            if matched:
                parent_line = items[index][1]
                while parent_line is not None and parent_line not in parsed:
                    parent_line = items[parent_line][1]
                parent = parsed.get(parent_line)
                task = read_task(index, self.lines[index], matched, section, parent)
                if parent is not None and (not active or active[-1] is not parent):
                    parent.uncertain_block = task.uncertain_block = True
                self.tasks.append(task)
                parsed[index] = task
                active.append(task)
            elif active:
                active[-1].note_lines.append(index)
        for task in self.tasks:
            task.block = "".join(self.lines[task.line : task.end])
            task.notes = self._notes(task)

    def _notes(self, task: Task) -> str:
        result = []
        previous = None
        for index in task.note_lines:
            if previous is not None and all(
                not line.strip() for line in self.lines[previous + 1 : index]
            ):
                result.extend("" for _ in range(index - previous - 1))
            result.append(without_eol(deindent(self.lines[index], task.note_indent)))
            previous = index
        return "\n".join(result)

    def snapshot(self) -> dict:
        show_root = self._show_root()
        return {
            "schema_version": 1,
            "revision": self.revision,
            "content_hash": self.content_hash,
            "sections": [
                section.snapshot() for section in self.sections if section.level or show_root
            ],
            "tasks": [task.snapshot() for task in self.tasks],
        }

    def changed_occurrence_ids(self, edited: TaskDocument) -> list[str]:
        def signatures(document: TaskDocument, task_id: str) -> Counter:
            return Counter(
                (
                    task.section.id,
                    task.section.ambiguous,
                    digest(task.block),
                    tuple(source_context(task)),
                    task.id,
                )
                for task in document.by_id.get(task_id, ())
            )

        return sorted(
            task_id
            for task_id in self.by_id.keys() | edited.by_id.keys()
            if signatures(self, task_id) != signatures(edited, task_id)
        )

    def _show_root(self) -> bool:
        return len(self.sections) == 1 or any(task.section.level == 0 for task in self.tasks)

    def _section(self, section_id: str) -> Section:
        sections = [section for section in self.sections if section.id == section_id]
        if not sections:
            raise DomainError("not_found", "Choose an existing source section.")
        if sections[0].ambiguous:
            raise DomainError("ambiguous_identity", "Choose a section with a unique heading path.")
        if section_id == "root" and not self._show_root():
            raise DomainError("not_found", "Choose an existing source section.")
        if (
            self.start
            and self.start == len(self.lines)
            and without_eol(self.lines[-1]) not in {"---", "..."}
        ):
            raise DomainError(
                "invalid_command", "Close YAML frontmatter before editing the task note."
            )
        return sections[0]

    def _lookup_id(self, task_id: str) -> Task:
        matches = self.by_id.get(task_id, [])
        if not matches:
            raise DomainError("not_found", "This task no longer exists.")
        if len(matches) > 1:
            raise DomainError("ambiguous_identity", "Resolve the duplicate Tasks ID in Obsidian.")
        if matches[0].id != task_id:
            raise DomainError("ambiguous_identity", "Resolve the malformed Tasks ID in Obsidian.")
        return matches[0]

    def _target(self, target: dict, base_revision: str) -> Task:
        if "id" in target:
            return self._lookup_id(target["id"])
        if base_revision != self.revision:
            raise DomainError("ambiguous_identity", "Refresh before editing a task without an ID.")
        task = self.by_line.get(target["line"])
        if not task or task.id is not None or digest(task.block) != target["source_hash"]:
            raise DomainError("ambiguous_identity", "The task without an ID changed or moved.")
        return task

    def _new_id(self, value: str):
        canonical_uuid(value)
        if value in self.by_id:
            raise DomainError("ambiguous_identity", "This Tasks ID already exists.")

    def _finish(
        self,
        lines: list[str],
        affected_lines: list[int],
        expected_total: int,
    ) -> tuple[Mutation, TaskDocument]:
        result = TaskDocument("".join(lines), self.revision)
        if len(result.tasks) != expected_total or any(
            index not in result.by_line or not result.by_line[index].title
            for index in affected_lines
        ):
            raise DomainError("invalid_command", "This edit changes the Markdown task structure.")
        return Mutation(
            result.content, [result.by_line[index].snapshot() for index in affected_lines]
        ), result

    def _insert(self, lines: list[str], index: int, block: list[str], local_eol: str):
        if index and not eol(lines[index - 1]):
            lines[index - 1] += local_eol
        if index < len(lines) and not eol(block[-1]):
            block[-1] += local_eol
        lines[index:index] = block

    def apply(self, command: dict) -> Mutation:
        command_id = canonical_uuid(command["command_id"])
        day = action_day(command["occurred_at"], command["action_timezone"])
        operation = command["operation"]
        kind = operation["type"]
        if kind == "create":
            return self._create(operation)
        if kind not in EXPECTED or not command.get("target"):
            raise DomainError("invalid_command", "Supply a supported operation and its target.")
        task = self._target(command["target"], command["base_revision"])
        expected = command.get("expected") or {}
        state = task.snapshot()
        if set(expected) != set(EXPECTED[kind]):
            raise DomainError("invalid_command", "Supply the expected state for this operation.")
        if any(expected[key] != state[key] for key in EXPECTED[kind]):
            raise DomainError(
                "revision_conflict", "The field or task block changed. Refresh and review it."
            )
        if not state["capabilities"][kind]:
            if task.uncertain_block and kind in {"edit_notes", "move", "delete"}:
                raise DomainError(
                    "invalid_command", "Edit this task's ambiguous Markdown list block in Obsidian."
                )
            if task.ambiguous_descendant and kind in {"move", "delete"}:
                raise DomainError(
                    "ambiguous_identity", "Resolve duplicate descendant Tasks IDs in Obsidian."
                )
            if task.unsupported_descendant and kind == "delete":
                raise DomainError(
                    "unsupported_rule",
                    "Delete this task in Obsidian to remove its recurring descendants.",
                )
            if task.unsupported_recurrence and kind in {"set_completion", "edit_due", "delete"}:
                raise DomainError("unsupported_rule", RECURRENCE_MESSAGE)
            raise DomainError("invalid_command", "This task does not support the requested edit.")
        if kind == "set_completion":
            return self._complete(task, operation["completed"], command_id, day)
        if kind == "edit_notes":
            return self._edit_notes(task, operation["notes"])
        if kind == "move":
            return self._move(task, operation["section_id"])
        if kind == "delete":
            if operation["descendant_count"] != task.descendant_count:
                raise DomainError(
                    "revision_conflict", "Confirm the current number of descendant tasks."
                )
            lines = self.lines[: task.line] + self.lines[task.end :]
            return self._finish(lines, [], len(self.tasks) - 1 - task.descendant_count)[0]
        if kind == "assign_id":
            self._new_id(operation["id"])
            header = task.edit_field("id", operation["id"])
        elif kind == "edit_title":
            value = check_title(operation["title"])
            if Counter(INLINE_TAG.findall(task.title)) - Counter(INLINE_TAG.findall(value)):
                raise DomainError(
                    "invalid_command", "Keep the existing inline tags in the task title."
                )
            header = task.header[: task.title_start] + value + task.header[task.title_end :]
        elif kind == "set_priority":
            header = task.edit_field("priority", check_priority(operation["priority"]))
        else:
            due = optional_date(operation["due"])
            if "repeat" in task.fields and (
                due is None or not due.endswith("-23") or due >= "9999-12-23"
            ):
                raise DomainError("unsupported_rule", RECURRENCE_MESSAGE)
            header = task.edit_field("due", due)
        lines = self.lines.copy()
        lines[task.line] = header + eol(lines[task.line])
        return self._finish(lines, [task.line], len(self.tasks))[0]

    def _create(self, operation: dict) -> Mutation:
        task_id = operation["id"]
        self._new_id(task_id)
        title = check_title(operation["title"])
        section = self._section(operation["section_id"])
        priority = check_priority(operation.get("priority"))
        due = optional_date(operation.get("due"))
        fields = {"id": task_id, "priority": priority, "due": due}
        suffix = "".join(f"  [{name}:: {fields[name]}]" for name in FIELD_ORDER if fields.get(name))
        lines = self.lines.copy()
        local_eol = (eol(lines[section.start - 1]) if section.start else "") or self.newline
        trailing = section.start < len(lines) or (lines and bool(eol(lines[-1])))
        self._insert(
            lines,
            section.start,
            [f"- [ ] {title}{suffix}" + (local_eol if trailing else "")],
            local_eol,
        )
        return self._finish(lines, [section.start], len(self.tasks) + 1)[0]

    def _move(self, task: Task, section_id: str) -> Mutation:
        section = self._section(section_id)
        if section is task.section:
            return Mutation(self.content, [task.snapshot()])
        if task.section.ambiguous:
            raise DomainError("ambiguous_identity", "Resolve the duplicate source section heading.")
        block = [deindent(line, task.indent) for line in self.lines[task.line : task.end]]
        lines = self.lines[: task.line] + self.lines[task.end :]
        index = section.start - (len(block) if task.line < section.start else 0)
        self._insert(lines, index, block, eol(block[0]) or self.newline)
        return self._finish(lines, [index], len(self.tasks))[0]

    def _edit_notes(self, task: Task, notes: str) -> Mutation:
        if not isinstance(notes, str):
            raise DomainError("invalid_command", "Enter Markdown notes as text.")
        if notes == task.notes:
            return Mutation(self.content, [task.snapshot()])
        removed = set(task.note_lines)
        for before, after in zip(task.note_lines, task.note_lines[1:]):
            if all(not line.strip() for line in self.lines[before + 1 : after]):
                removed.update(range(before + 1, after))
        index = min(removed, default=task.line + 1)
        lines = [line for position, line in enumerate(self.lines) if position not in removed]
        if notes:
            local_eol = eol(self.lines[task.line]) or self.newline
            block = [
                (" " * task.note_indent + line if line else "") + local_eol
                for line in LINE_BREAK.split(notes.rstrip("\r\n"))
            ]
            self._insert(lines, index, block, local_eol)
        mutation, result = self._finish(lines, [task.line], len(self.tasks))
        edited = result.by_line[task.line]
        if [(item.header, item.depth, item.section.id) for item in self.tasks] != [
            (item.header, item.depth, item.section.id) for item in result.tasks
        ] or edited.notes != "\n".join(LINE_BREAK.split(notes.rstrip("\r\n"))):
            raise DomainError(
                "invalid_command", "Notes must preserve the existing Markdown task structure."
            )
        return mutation

    def _complete(self, task: Task, completed: bool, command_id: str, day: date) -> Mutation:
        if task.completed == completed:
            return Mutation(self.content, [task.snapshot()])
        lines = self.lines.copy()
        header = (
            task.header[: task.status_start]
            + ("x" if completed else " ")
            + task.header[task.status_start + 1 :]
        )
        original_eol = eol(lines[task.line])
        lines[task.line] = (
            task.edit_field("completion", day.isoformat() if completed else None, header)
            + original_eol
        )
        successor = completed and task.recurrence_supported
        if successor:
            successor_id = str(uuid5(UUID(command_id), "recurrence-successor"))
            self._new_id(successor_id)
            due = parse_date(task.get("due"))
            year = due.year + (due.month == 12)
            month = due.month % 12 + 1
            next_due = due.replace(year=year, month=month).isoformat()
            new_fields = {name: field.value for name, field in task.fields.items()}
            for key in ("created", "completion", "cancelled", "dependsOn"):
                new_fields.pop(key, None)
            new_fields.update(id=successor_id, due=next_due)
            parts = [
                task.header[: task.title_start],
                task.title,
                *[f"  [{name}:: {new_fields[name]}]" for name in FIELD_ORDER if name in new_fields],
            ]
            tail_tags = self._trailing_tags(task)
            if tail_tags:
                parts.insert(2, tail_tags)
            lines.insert(task.line, "".join(parts) + (original_eol or self.newline))
        completed_line = task.line + int(successor)
        affected = [completed_line] + ([task.line] if successor else [])
        mutation, result = self._finish(lines, affected, len(self.tasks) + int(successor))
        if completed:
            completed_task = result.by_line[completed_line]
            next_task = result.by_line[task.line] if successor else None
            mutation.undo = {
                "completed": undo_guard(completed_task),
                "completed_context": source_context(completed_task),
                "successor": undo_guard(next_task) if next_task else None,
                "successor_context": source_context(next_task) if next_task else None,
                "previous_header": task.header,
                "result_hash": result.content_hash,
            }
        return mutation

    def _trailing_tags(self, task: Task) -> str:
        tail = task.suffix_end
        tags = []
        while tail > task.title_end:
            matching_field = next(
                (item for item in task.fields.values() if item.end == tail),
                None,
            )
            if matching_field:
                tail = matching_field.start
                continue
            tag = TRAILING_TAG.search(task.header, task.title_end, tail)
            if tag is None:
                break
            tags.append(tag[0])
            tail = tag.start()
        return "".join(reversed(tags))

    def _undo_occurrence(self, guard: dict, context: list[str], result_hash: str) -> Task:
        try:
            task = (
                self._lookup_id(guard["id"])
                if guard["id"] is not None
                else self._target(
                    {"line": guard["line"], "source_hash": guard["source_hash"]}, self.revision
                )
            )
        except DomainError as error:
            raise DomainError(
                "revision_conflict", "The completion occurrence is missing or ambiguous."
            ) from error
        if (
            task.id is None
            and self.content_hash != result_hash
            or task.section.ambiguous
            or task.section.id != guard["section_id"]
            or digest(task.block) != guard["source_hash"]
            or source_context(task) != context
        ):
            raise DomainError("revision_conflict", "The completed task or its successor changed.")
        return task

    def undo(
        self,
        base_revision: str,
        completion: dict,
        expected_completed: dict,
        expected_successor: dict | None,
    ) -> Mutation:
        if (
            expected_completed != completion["completed"]
            or expected_successor != completion["successor"]
        ):
            raise DomainError(
                "revision_conflict", "Undo must identify the original completion and successor."
            )
        if base_revision != self.revision:
            raise DomainError("revision_conflict", "Refresh before undoing the completion.")
        task = self._undo_occurrence(
            expected_completed,
            completion["completed_context"],
            completion["result_hash"],
        )
        if not task.completed or task.issues:
            raise DomainError("revision_conflict", "The original task is no longer completed.")
        lines = self.lines.copy()
        lines[task.line] = completion["previous_header"] + eol(lines[task.line])
        reopened = task.line
        removed = 0
        if expected_successor is not None:
            successor = self._undo_occurrence(
                expected_successor,
                completion["successor_context"],
                completion["result_hash"],
            )
            del lines[successor.line : successor.end]
            reopened -= (successor.end - successor.line) if successor.line < task.line else 0
            removed = 1
        return self._finish(lines, [reopened], len(self.tasks) - removed)[0]
