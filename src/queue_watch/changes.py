"""Changes: records from every source that describe the same branch or commit.

Records correlate by ``ref`` and fall back to ``sha``; records with neither stay
in their source's own tab. When records name more than one ``repo``, the same ref
in two repositories is two changes, and records without a ``repo`` form changes of
their own. Kinds with a role in a change: ``run`` (CI),
``queue_entry`` (live merge queue position) and ``attempt`` (a finished merge
attempt). Other kinds are only shown per source.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import NamedTuple

# Worst first: decides the one state that represents a whole change.
PRIORITY = ("failure", "blocked", "running", "queued", "cancelled", "success", "neutral", "unknown")
GROUPS = (
    ("attention", "Needs attention", "blocked"),
    ("running", "Running", "running"),
    ("queued", "Queued", "queued"),
    ("done", "Done", "success"),
)
TIME_KEYS = ("created_at", "updated_at", "started_at", "finished_at")
ROLES = frozenset({"run", "queue_entry", "attempt"})


class Hit(NamedTuple):
    """One record and the id of the source that reported it."""

    source: str
    record: dict


def worst(states: list[str]) -> str | None:
    return min(states, key=PRIORITY.index, default=None)


def stamp(record: dict, *keys: str) -> float:
    """Latest of ``keys`` (all timestamps by default) as epoch seconds; 0 if none."""
    times = [
        datetime.fromisoformat(record[key]).timestamp()
        for key in keys or TIME_KEYS
        if record.get(key)
    ]
    return max(times, default=0.0)


@dataclass
class Change:
    key: str
    ref: str | None = None
    sha: str | None = None
    repo: str | None = None  # set only when the snapshots span several repositories
    runs: list[Hit] = field(default_factory=list)  # newest first
    entries: list[Hit] = field(default_factory=list)  # queue order
    attempts: list[Hit] = field(default_factory=list)  # newest first

    @property
    def entry(self) -> Hit | None:
        return self.entries[0] if self.entries else None

    @property
    def checks(self) -> list[Hit]:
        """The newest run of each workflow: what CI currently says about this change."""
        latest: dict[tuple[str, str], Hit] = {}
        for hit in self.runs:
            latest.setdefault((hit.source, hit.record["title"]), hit)
        return list(latest.values())

    @property
    def ci_state(self) -> str | None:
        return worst([hit.record["state"] for hit in self.checks])

    @property
    def live_states(self) -> list[str]:
        states = [self.ci_state] if self.runs else []
        return states + ([self.entry.record["state"]] if self.entry else [])

    @property
    def state(self) -> str:
        if self.live_states:
            return worst(self.live_states)
        return self.attempts[0].record["state"] if self.attempts else "unknown"

    @property
    def group(self) -> str:
        live = set(self.live_states)
        if live & {"failure", "blocked"}:
            return "attention"
        if "running" in live:
            return "running"
        if "queued" in live:
            return "queued"
        return "done"

    @property
    def title(self) -> str:
        for hit in (self.entry, *self.attempts):
            if hit:
                return re.sub(r"^#\d+\s+", "", hit.record["title"])
        newest = self.runs[0].record if self.runs else {}
        return newest.get("subtitle") or newest.get("title") or self.key

    @property
    def pr(self) -> int | None:
        """The pull request this change belongs to, from a queue entry, a run, a title."""
        for hit in (self.entry, *self.runs, *self.attempts):
            if hit and isinstance(
                number := (hit.record.get("fields") or {}).get("pull_request"), int
            ):
                return number
        for hit in (self.entry, *self.attempts):
            if hit and (match := re.match(r"#(\d+)\b", hit.record["title"])):
                return int(match.group(1))
        return None

    @property
    def owner(self) -> str | None:
        for hit in (self.entry, *self.attempts, *self.runs):
            if hit and hit.record.get("owner"):
                return hit.record["owner"]
        return None

    @property
    def url(self) -> str | None:
        for hit in (self.entry, *self.runs, *self.attempts):
            if hit and hit.record.get("url"):
                return hit.record["url"]
        return None

    @property
    def activity(self) -> float:
        hits = (*self.runs, *self.entries, *self.attempts)
        return max((stamp(hit.record) for hit in hits), default=0.0)


def correlate(snapshots: dict[str, dict]) -> list[Change]:
    """One Change per repository and ref (falling back to sha), in merge order, then
    most recent. Repositories only separate changes when more than one appears."""
    # GitHub repository names are case-insensitive.
    repos = {
        record["repo"].casefold()
        for snapshot in snapshots.values()
        for record in snapshot.get("records", [])
        if record.get("repo")
    }
    several = len(repos) > 1
    by_ref: dict[tuple[str | None, str], Change] = {}
    by_sha: dict[tuple[str | None, str], Change] = {}
    changes: list[Change] = []
    for source, snapshot in snapshots.items():
        for record in snapshot.get("records", []):
            ref, sha = record.get("ref"), record.get("sha")
            if record["kind"] not in ROLES or not (ref or sha):
                continue
            repo = (record.get("repo") or None) if several else None
            scope = repo and repo.casefold()
            change = (ref and by_ref.get((scope, ref))) or (sha and by_sha.get((scope, sha)))
            if not change:
                name = ref or sha
                change = Change(key=f"{scope}:{name}" if scope else name, repo=repo)
                changes.append(change)
            if ref:
                by_ref.setdefault((scope, ref), change)
                change.ref = change.ref or ref
            if sha:
                by_sha.setdefault((scope, sha), change)
                change.sha = change.sha or sha
            hit = Hit(source, record)
            match record["kind"]:
                case "run":
                    change.runs.append(hit)
                case "queue_entry":
                    change.entries.append(hit)
                case "attempt":
                    change.attempts.append(hit)
    for change in changes:
        change.runs.sort(key=lambda hit: stamp(hit.record, "created_at"), reverse=True)
        change.entries.sort(key=lambda hit: position(hit.record))
        change.attempts.sort(key=lambda hit: stamp(hit.record), reverse=True)
    changes.sort(key=lambda c: (position(c.entry.record) if c.entry else 1e9, -c.activity))
    return changes


def position(record: dict) -> float:
    value = record.get("position")
    return value if isinstance(value, int) else 1e9


def grouped(changes: list[Change]) -> list[tuple[str, str, str, list[Change]]]:
    """(key, label, representative state, changes) per group, in display order."""
    return [
        (key, label, state, [change for change in changes if change.group == key])
        for key, label, state in GROUPS
    ]
