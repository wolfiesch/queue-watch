"""Snapshot contract v1: the only shape views consume and adapters produce.

A snapshot is one observation of one source:

    {
      "schema": "queue-watch.snapshot/1",
      "observed_at": "<ISO 8601 with offset>",
      "records": [Record, ...],
      "views": [View, ...],        # optional; defaults come from DEFAULT_VIEWS
      "warnings": ["...", ...],    # optional; partial data, truncation, skipped input
      "note": "..."                # optional; caveats shown in the inspector
    }

Record fields: ``id``, ``kind``, ``state`` and ``title`` are required. ``state`` is
one of STATES; adapters keep their own vocabulary in ``raw_state``. ``ref`` and
``sha`` identify the change so records from different sources can be correlated;
``repo`` ("owner/name") keeps equal refs from different repositories apart.
Optional: ``raw_state``, ``subtitle``, ``ref``, ``sha``, ``repo``, ``owner``,
``position`` (int), ``created_at``, ``updated_at``, ``started_at``, ``finished_at``
(ISO 8601), ``url``, ``fields`` (JSON object for the inspector) and ``children``
(records, for example jobs and steps).

View: ``{"kind", "label", "columns": [{"key", "label", "width", "format"}]}``.
A column key is a record field name or ``fields.<name>``; FORMATS lists formats.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

SCHEMA = "queue-watch.snapshot/1"
STATES = (
    "queued",
    "running",
    "blocked",
    "success",
    "failure",
    "cancelled",
    "neutral",
    "unknown",
)
ACTIVE = frozenset({"queued", "running", "blocked"})
ATTENTION = frozenset({"failure", "blocked"})
FORMATS = ("text", "state", "clock", "age", "seconds", "duration", "sha")
TIME_FIELDS = ("created_at", "updated_at", "started_at", "finished_at")
TEXT_FIELDS = ("raw_state", "subtitle", "ref", "sha", "repo", "owner", "url")

DEFAULT_VIEWS: dict[str, dict] = {
    "run": {
        "kind": "run",
        "label": "Runs",
        "columns": [
            {"key": "state", "label": "State", "width": 12, "format": "state"},
            {"key": "title", "label": "Workflow / title", "width": 25},
            {"key": "ref", "label": "Branch", "width": 18},
            {"key": "sha", "label": "SHA", "width": 8, "format": "sha"},
            {"key": "created_at", "label": "Age", "width": 9, "format": "age"},
        ],
    },
    "queue_entry": {
        "kind": "queue_entry",
        "label": "Queue",
        "columns": [
            {"key": "position", "label": "#", "width": 4},
            {"key": "state", "label": "State", "width": 12, "format": "state"},
            {"key": "title", "label": "Change", "width": 38},
            {"key": "owner", "label": "Owner", "width": 19},
            {"key": "created_at", "label": "Age", "width": 10, "format": "age"},
        ],
    },
    "attempt": {
        "kind": "attempt",
        "label": "Recent attempts",
        "columns": [
            {"key": "state", "label": "Outcome", "width": 10, "format": "state"},
            {"key": "title", "label": "Change", "width": 38},
            {"key": "finished_at", "label": "Finished", "width": 18, "format": "clock"},
        ],
    },
}


class SnapshotError(ValueError):
    """A snapshot does not satisfy contract v1."""


def _iso(value: Any, where: str) -> None:
    if not isinstance(value, str):
        raise SnapshotError(f"{where} must be an ISO 8601 string")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise SnapshotError(f"{where} is not ISO 8601: {value!r}") from error
    if parsed.tzinfo is None:
        raise SnapshotError(f"{where} needs a UTC offset: {value!r}")


def validate_record(record: Any, where: str = "record") -> None:
    if not isinstance(record, dict):
        raise SnapshotError(f"{where} must be an object")
    for key in ("id", "kind", "title"):
        if not isinstance(record.get(key), str) or not record[key]:
            raise SnapshotError(f"{where}.{key} must be a non-empty string")
    if record.get("state") not in STATES:
        raise SnapshotError(
            f"{where}.state must be one of {', '.join(STATES)}, got {record.get('state')!r}"
        )
    for key in TEXT_FIELDS:
        if record.get(key) is not None and not isinstance(record[key], str):
            raise SnapshotError(f"{where}.{key} must be a string")
    position = record.get("position")
    if position is not None and (isinstance(position, bool) or not isinstance(position, int)):
        raise SnapshotError(f"{where}.position must be an integer")
    for key in TIME_FIELDS:
        if record.get(key) is not None:
            _iso(record[key], f"{where}.{key}")
    if record.get("fields") is not None and not isinstance(record["fields"], dict):
        raise SnapshotError(f"{where}.fields must be an object")
    children = record.get("children")
    if children is not None:
        if not isinstance(children, list):
            raise SnapshotError(f"{where}.children must be a list")
        for index, child in enumerate(children):
            validate_record(child, f"{where}.children[{index}]")


def validate_snapshot(snapshot: Any) -> dict:
    """Return ``snapshot`` unchanged if it satisfies contract v1, else raise."""
    if not isinstance(snapshot, dict):
        raise SnapshotError("snapshot must be an object")
    if snapshot.get("schema") != SCHEMA:
        raise SnapshotError(f"schema must be {SCHEMA!r}, got {snapshot.get('schema')!r}")
    _iso(snapshot.get("observed_at"), "observed_at")
    records = snapshot.get("records")
    if not isinstance(records, list):
        raise SnapshotError("records must be a list")
    seen = set()
    for index, record in enumerate(records):
        validate_record(record, f"records[{index}]")
        if record["id"] in seen:
            raise SnapshotError(f"records[{index}].id {record['id']!r} is duplicated")
        seen.add(record["id"])
    for key in ("warnings",):
        value = snapshot.get(key, [])
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            raise SnapshotError(f"{key} must be a list of strings")
    if snapshot.get("note") is not None and not isinstance(snapshot["note"], str):
        raise SnapshotError("note must be a string")
    for index, view in enumerate(snapshot.get("views", [])):
        where = f"views[{index}]"
        if not isinstance(view, dict) or not isinstance(view.get("kind"), str):
            raise SnapshotError(f"{where}.kind must be a string")
        columns = view.get("columns")
        if not isinstance(columns, list) or not columns:
            raise SnapshotError(f"{where}.columns must be a non-empty list")
        for number, column in enumerate(columns):
            if not isinstance(column, dict) or not isinstance(column.get("key"), str):
                raise SnapshotError(f"{where}.columns[{number}].key must be a string")
            if column.get("format", "text") not in FORMATS:
                raise SnapshotError(
                    f"{where}.columns[{number}].format must be one of {', '.join(FORMATS)}"
                )
    return snapshot


def views_for(snapshot: dict) -> list[dict]:
    """Declared views first, then defaults for any record kind left undeclared."""
    views = list(snapshot.get("views", []))
    declared = {view["kind"] for view in views}
    for record in snapshot.get("records", []):
        kind = record["kind"]
        if kind not in declared:
            declared.add(kind)
            views.append(
                DEFAULT_VIEWS.get(kind)
                or {
                    "kind": kind,
                    "label": kind.replace("_", " ").capitalize(),
                    "columns": DEFAULT_VIEWS["run"]["columns"],
                }
            )
    return views


def lookup(record: dict, key: str) -> Any:
    if key.startswith("fields."):
        return (record.get("fields") or {}).get(key.removeprefix("fields."))
    return record.get(key)
