#!/usr/bin/env python3
"""queue-watch visual-direction mockups (design artifact, not the product).

Two static Textual dashboards rendered from design/fixtures/demo.json, so the
owner can pick a direction before the real UI is redesigned:

    dense   control-room: tight table rows, inspector as a right column.
    calm    card per change with a compact pipeline line, inspector as a drawer.

Usage (from the repo root):

    uv run python design/mockups/mockup.py --variant dense                # interactive
    uv run python design/mockups/mockup.py --variant calm --theme nord
    uv run python design/mockups/mockup.py --variant calm --ascii         # no Unicode glyphs
    uv run python design/mockups/mockup.py --variant dense --theme tokyo-night \\
        --screenshot design/mockups/screens/dense-tokyo-night-160x45.svg --size 160x45

Ideas demonstrated by both variants (each in its own style):

    1. Changes view: one row per change, CI runs + merge-queue entry + attempts
       correlated by ``ref`` (or ``sha``), grouped Needs attention / Running /
       Queued / Done. Per-source tables stay reachable as secondary tabs (1/2/3).
    2. The queue is a FIFO chain of chips; position 1 is highlighted; ETA comes
       from ``fields.estimated_seconds``.
    3. One-line status bar with a pill per source: label, live/stale, age.
    4. Glyph + colour state vocabulary that reads without colour (``--ascii`` swaps
       the glyphs for plain ASCII).
    5. Inspector: CI / Queue / History sections, job tree with duration bars
       (``fields.duration_seconds``), steps only for running or failed jobs.
    6. Trend: Sparkline of ``fields.lock_hold_seconds`` plus count summaries.
    7. Every colour is a Textual theme variable ($primary, $error, ...), so
       ``--theme`` restyles everything. No hex literals anywhere in this file.

The feed is frozen at the fixture's ``observed_at``; nothing polls or animates.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import statistics
import sys
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from rich.style import Style as RichStyle
from rich.text import Text
from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.color import Color
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.content import Content
from textual.events import Resize
from textual.message import Message
from textual.widgets import (
    Button,
    DataTable,
    Footer,
    OptionList,
    Sparkline,
    Static,
    TabbedContent,
    TabPane,
)
from textual.widgets.option_list import Option, OptionDoesNotExist

HERE = Path(__file__).resolve().parent
DEFAULT_FIXTURE = HERE.parent / "fixtures" / "demo.json"

# A source whose last poll is older than this is shown as "stale".
STALE_AFTER_SECONDS = 60
# The fixture has no poll lag of its own, so the mockup simulates one per source
# (override with --lag SOURCE=SECONDS) to show both pill states.
DEFAULT_LAG = {"ci": 4, "merge-queue": 95}


# --------------------------------------------------------------------------- #
# State vocabulary: glyph + theme colour. Glyphs alone must carry the meaning. #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Symbols:
    glyphs: dict[str, str]
    arrow: str
    tee: str
    elbow: str
    pipe: str
    bar_full: str
    bar_empty: str
    dot: str
    rule: str
    border: str  # Textual border type used for rounded chrome


UNICODE = Symbols(
    glyphs={
        "running": "●",
        "queued": "◌",
        "success": "✓",
        "failure": "✗",
        "cancelled": "⊘",
        "blocked": "!",
        "neutral": "·",
        "unknown": "·",
    },
    arrow="→",
    tee="├",
    elbow="└",
    pipe="│",
    bar_full="█",
    bar_empty="░",
    dot="·",
    rule="─",
    border="round",
)
ASCII = Symbols(
    glyphs={
        "running": "*",
        "queued": "o",
        "success": "+",
        "failure": "x",
        "cancelled": "/",
        "blocked": "!",
        "neutral": ".",
        "unknown": ".",
    },
    arrow="->",
    tee="|-",
    elbow="`-",
    pipe="| ",
    bar_full="#",
    bar_empty="-",
    dot="|",
    rule="-",
    border="ascii",
)
SYMBOLS = UNICODE  # replaced in main() when --ascii is given

# Content style strings: Textual resolves "$name" against the active theme.
STATE_STYLE = {
    "running": "$primary",
    "queued": "$text-muted",
    "success": "$success",
    "failure": "$error",
    "cancelled": "$text-muted",
    "blocked": "$warning",
    "neutral": "$text-muted",
    "unknown": "$text-muted",
}
MUTED = "$text-muted"

# Worst first: decides the one glyph that represents a whole change.
STATE_PRIORITY = ("failure", "blocked", "running", "queued", "cancelled", "success", "neutral", "unknown")

GROUPS = (
    ("attention", "Needs attention", "blocked"),
    ("running", "Running", "running"),
    ("queued", "Queued", "queued"),
    ("done", "Done", "success"),
)


def glyph(state: str) -> tuple[str, str]:
    """(glyph, style) for a normalized state, for ``Content.assemble``."""
    return SYMBOLS.glyphs.get(state, SYMBOLS.glyphs["unknown"]), STATE_STYLE.get(state, MUTED)


def muted(text: str) -> tuple[str, str]:
    return text, MUTED


def dim(text: str) -> Content:
    return Content.assemble(muted(text))


# --------------------------------------------------------------------------- #
# Feed: load the fixture and correlate records into changes.                   #
# --------------------------------------------------------------------------- #


def parse_time(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def human_seconds(seconds: float) -> str:
    seconds = round(seconds)
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        minutes, rest = divmod(seconds, 60)
        return f"{minutes}m{rest:02d}s" if rest else f"{minutes}m"
    hours, rest = divmod(seconds, 3600)
    return f"{hours}h{rest // 60:02d}m"


def eta_text(seconds: float) -> str:
    """Estimates are rough: whole minutes once past a minute and a half, ``~7m``."""
    return f"~{human_seconds(seconds if seconds < 90 else round(seconds / 60) * 60)}"


def short_age(seconds: float) -> str:
    """Coarse age for table cells: 45s, 12m, 3h."""
    seconds = max(0, round(seconds))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    return f"{seconds // 3600}h"


@dataclass
class Source:
    key: str
    label: str
    adapter: str
    records: list[dict]
    note: str
    lag: int  # simulated seconds since the last successful poll

    @property
    def stale(self) -> bool:
        return self.lag > STALE_AFTER_SECONDS


@dataclass
class Change:
    """Everything the sources know about one ref."""

    ref: str
    sha: str | None = None
    runs: list[dict] = field(default_factory=list)  # newest first
    entry: dict | None = None  # merge-queue entry (live)
    attempts: list[dict] = field(default_factory=list)  # merge-queue history, newest first

    @property
    def latest_run(self) -> dict | None:
        return self.runs[0] if self.runs else None

    @property
    def title(self) -> str:
        if self.latest_run and self.latest_run.get("subtitle"):
            return self.latest_run["subtitle"]
        for record in (self.entry, *self.attempts):
            if record:
                return re.sub(r"^#\d+\s+", "", record["title"])
        return self.latest_run["title"] if self.latest_run else self.ref

    @property
    def pr(self) -> int | None:
        if self.entry and (pr := (self.entry.get("fields") or {}).get("pull_request")):
            return pr
        for record in (self.entry, *self.attempts):
            if record and (match := re.match(r"#(\d+)", record["title"])):
                return int(match.group(1))
        return None

    @property
    def owner(self) -> str | None:
        return self.entry.get("owner") if self.entry else None

    @property
    def url(self) -> str | None:
        for record in (self.entry, self.latest_run):
            if record and record.get("url"):
                return record["url"]
        return None

    @property
    def live_states(self) -> list[str]:
        return [r["state"] for r in (self.latest_run, self.entry) if r]

    @property
    def state(self) -> str:
        """The single state shown next to the change: worst of CI and queue."""
        states = self.live_states or [a["state"] for a in self.attempts[:1]]
        return min(states, key=STATE_PRIORITY.index, default="unknown")

    @property
    def group(self) -> str:
        live = self.live_states
        if "failure" in live or "blocked" in live:
            return "attention"
        if "running" in live:
            return "running"
        if "queued" in live:
            return "queued"
        return "done"

    @property
    def activity(self) -> datetime:
        stamps = [t for r in (*self.runs, self.entry, *self.attempts) if r for t in record_times(r)]
        return max(stamps)


def record_times(record: dict) -> list[datetime]:
    keys = ("created_at", "updated_at", "started_at", "finished_at")
    return [t for key in keys if (t := parse_time(record.get(key)))]


@dataclass
class Feed:
    now: datetime
    sources: dict[str, Source]
    changes: list[Change]

    def counts(self) -> dict[str, int]:
        return {key: sum(c.group == key for c in self.changes) for key, _, _ in GROUPS}

    def grouped(self) -> list[tuple[str, str, str, list[Change]]]:
        return [
            (key, name, state, [c for c in self.changes if c.group == key]) for key, name, state in GROUPS
        ]

    def change(self, ref: str) -> Change:
        return next(c for c in self.changes if c.ref == ref)

    def entries(self) -> list[dict]:
        """Live merge-queue entries in FIFO order."""
        entries = [c.entry for c in self.changes if c.entry]
        return sorted(entries, key=lambda e: e["position"])

    def attempts(self) -> list[dict]:
        """Finished merge attempts, oldest first (trend order)."""
        attempts = [a for c in self.changes for a in c.attempts]
        return sorted(attempts, key=lambda a: parse_time(a["finished_at"]))

    def age(self, when: str | datetime | None) -> float | None:
        stamp = parse_time(when) if isinstance(when, str) or when is None else when
        return (self.now - stamp).total_seconds() if stamp else None


def load_feed(path: Path, lag: dict[str, int]) -> Feed:
    data = json.loads(path.read_text())
    sources = {
        key: Source(
            key=key,
            label=raw["label"],
            adapter=raw["adapter"],
            records=raw["snapshot"]["records"],
            note=raw["snapshot"].get("note", ""),
            lag=lag.get(key, 0),
        )
        for key, raw in data["sources"].items()
    }
    return Feed(now=parse_time(data["observed_at"]), sources=sources, changes=correlate(sources))


def correlate(sources: dict[str, Source]) -> list[Change]:
    """Merge records from every source into one Change per ref (falling back to sha).

    The mockup keys on the fixture's record kinds: ``run`` is CI, ``queue_entry`` and
    ``attempt`` are the merge queue. The real app will learn this from adapters.
    """
    by_ref: dict[str, Change] = {}
    by_sha: dict[str, Change] = {}
    for source in sources.values():
        for record in source.records:
            ref, sha = record.get("ref"), record.get("sha")
            if not ref and not sha:
                continue
            change = by_ref.get(ref) or by_sha.get(sha) or Change(ref=ref or sha[:7])
            if ref:
                by_ref[ref] = change
            if sha:
                by_sha[sha] = change
                change.sha = change.sha or sha
            match record["kind"]:
                case "run":
                    change.runs.append(record)
                case "queue_entry":
                    change.entry = record
                case "attempt":
                    change.attempts.append(record)
    changes = list({id(c): c for c in (*by_ref.values(), *by_sha.values())}.values())
    for change in changes:
        change.runs.sort(key=lambda r: parse_time(r["created_at"]), reverse=True)
        change.attempts.sort(key=lambda a: parse_time(a["finished_at"]), reverse=True)
    # Queue order first (it is the merge order), then most recent activity.
    changes.sort(key=lambda c: (c.entry["position"] if c.entry else 99, -c.activity.timestamp()))
    return changes


# --------------------------------------------------------------------------- #
# Per-source status snippets, shared by both variants.                         #
# --------------------------------------------------------------------------- #


def notable_job(run: dict) -> dict | None:
    jobs = run.get("children") or []
    for state in ("failure", "blocked", "running", "queued"):
        for job in jobs:
            if job["state"] == state:
                return job
    return None


def ci_detail(run: dict) -> str:
    if job := notable_job(run):
        return job["title"]
    if jobs := run.get("children"):
        return f"{len(jobs)} job{'s' if len(jobs) != 1 else ''}"
    return run.get("raw_state", run["state"]).replace("_", " ")


def eta_seconds(entry: dict | None) -> float | None:
    return ((entry or {}).get("fields") or {}).get("estimated_seconds")


def ci_status(change: Change, label: str = "CI") -> Content:
    """``CI ✗ test (3.12)``"""
    run = change.latest_run
    if not run:
        return Content.assemble((f"{label} —", MUTED))
    return Content.assemble((f"{label} ", MUTED), glyph(run["state"]), " ", ci_detail(run))


def queue_status(change: Change, label: str = "Queue", with_eta: bool = True) -> Content:
    """``Queue ! #4 blocked ~7m`` or, for finished attempts, ``Queue ✓ merged 38s`` (lock hold)."""
    if entry := change.entry:
        parts: list[Any] = [(f"{label} ", MUTED), glyph(entry["state"]), f" #{entry['position']} {entry['state']}"]
        if with_eta and (eta := eta_seconds(entry)):
            parts.append(muted(f" {eta_text(eta)}"))
        return Content.assemble(*parts)
    if change.attempts:
        attempt = change.attempts[0]
        hold = (attempt.get("fields") or {}).get("lock_hold_seconds")
        parts = [(f"{label} ", MUTED), glyph(attempt["state"]), f" {attempt['raw_state']}"]
        if with_eta and hold:
            parts.append(muted(f" {human_seconds(hold)}"))
        return Content.assemble(*parts)
    return Content.assemble((f"{label} —", MUTED))


def fit(content: Content, width: int, align: str = "left") -> Content:
    content = content.truncate(width, ellipsis=True)
    gap = width - content.cell_length
    return content.pad_left(gap) if align == "right" and gap > 0 else content.pad_right(max(gap, 0))


def short_ref(ref: str) -> str:
    return ref.rsplit("/", 1)[-1]


# --------------------------------------------------------------------------- #
# Messages                                                                     #
# --------------------------------------------------------------------------- #


class ChangeSelected(Message):
    """A list, card, chip or table row asked for this change to be inspected."""

    def __init__(self, ref: str) -> None:
        super().__init__()
        self.ref = ref


# --------------------------------------------------------------------------- #
# Shared widgets                                                               #
# --------------------------------------------------------------------------- #


class StatusBar(Horizontal):
    """One line: brand, a pill per source (label, live/stale, age), count summary."""

    def __init__(self, feed: Feed) -> None:
        super().__init__(id="status")
        self.feed = feed

    def compose(self) -> ComposeResult:
        yield Static(Content.assemble(("queue-watch", "b")), id="brand")
        for source in self.feed.sources.values():
            mark = ("!", "b") if source.stale else ("✓" if SYMBOLS is UNICODE else "+", "b")
            word = "stale" if source.stale else "live"
            yield Static(
                Content.assemble(mark, f" {source.label} ", (f"{word} {human_seconds(source.lag)}", "b")),
                classes=f"pill {word}",
            )
        yield Static(self.counts(words=True), id="counts-long")
        yield Static(self.counts(words=False), id="counts-short")

    def counts(self, words: bool) -> Content:
        """``! 2 attention   ● 2 running ...``, or just ``! 2  ● 2 ...`` when space is tight."""
        counts = self.feed.counts()
        parts: list[Any] = []
        for key, _, state in GROUPS:
            parts += [glyph(state), f" {counts[key]} {key}" if words else f" {counts[key]}", "   " if words else "  "]
        return Content.assemble(*parts[:-1])


class QueueChip(Static):
    """One merge-queue entry in the FIFO chain. Position 1 holds the lock."""

    def __init__(self, entry: dict, change: Change, variant: str) -> None:
        holder = entry["position"] == 1
        super().__init__(classes="chip holder" if holder else "chip")
        self.ref = change.ref
        self.entry, self.change, self.variant, self.holder = entry, change, variant, holder
        eta = eta_seconds(entry)
        self.eta = eta_text(eta) if eta else "ETA —"

    def on_mount(self) -> None:
        position = self.entry["position"]
        if self.variant == "calm":
            self.border_title = f"{position} · next" if self.holder else str(position)
        self.update(self.dense_content() if self.variant == "dense" else self.calm_content())

    def style_for(self, state: str) -> tuple[str, str]:
        # A highlighted chip has a solid $primary fill: colour spans would vanish on it.
        text, style = glyph(state)
        return (text, "") if self.holder and self.variant == "dense" else (text, style)

    def dense_content(self) -> Content:
        pos = self.entry["position"]
        return Content.assemble(
            (f" {pos} ", "b"),
            self.style_for(self.entry["state"]),
            f" #{self.change.pr} ",
            (self.eta, "b"),
            (f"  {short_ref(self.change.ref)}", "" if self.holder else MUTED),
            " ",
        )

    def calm_content(self) -> Content:
        return Content.assemble(
            (f"#{self.change.pr} {self.change.title}", "b"),
            "\n",
            glyph(self.entry["state"]),
            f" {self.entry['raw_state'].replace('_', ' ')}",
            muted(f" {SYMBOLS.dot} "),
            (self.eta, "b" if self.holder else ""),
        )

    def on_click(self) -> None:
        self.post_message(ChangeSelected(self.ref))


class QueueStrip(Vertical):
    """Header (ETA + lock-hold trend) over a FIFO chain of chips."""

    def __init__(self, feed: Feed, variant: str) -> None:
        super().__init__(id="queue")
        self.feed, self.variant = feed, variant

    def compose(self) -> ComposeResult:
        entries = self.feed.entries()
        holds = [a["fields"]["lock_hold_seconds"] for a in self.feed.attempts()]
        failed = sum(a["state"] == "failure" for a in self.feed.attempts())
        clears = max((eta_seconds(e) or 0 for e in entries), default=0)

        title = Content.assemble(("MERGE QUEUE", "b"), muted(f"  FIFO · {len(entries)} waiting"))
        eta = Content.assemble(muted("clears in "), (eta_text(clears), "b")) if clears else Content("")
        trend = Content.assemble(
            muted("lock hold "),
        )
        stats = Content.assemble(
            muted(f" median {human_seconds(statistics.median(holds))} {SYMBOLS.dot} max {human_seconds(max(holds))}"),
            muted(f" {SYMBOLS.dot} "),
            glyph("failure"),
            muted(f" {failed}/{len(holds)}"),
        )
        with Horizontal(id="queue-head"):
            yield Static(title, id="queue-title")
            yield Static(eta, id="queue-eta")
            yield Static(trend, id="trend-label")
            spark = Sparkline(holds, id="trend")
            spark.styles.width = 2 * len(holds)  # two cells per attempt: thicker, easier to read bars
            yield spark
            yield Static(stats, id="trend-stats")
        with Horizontal(id="chain"):
            for index, entry in enumerate(entries):
                if index:
                    yield Static(SYMBOLS.arrow, classes="link")
                change = next(c for c in self.feed.changes if c.entry is entry)
                yield QueueChip(entry, change, self.variant)


# --- inspector ------------------------------------------------------------- #


class Inspector(VerticalScroll):
    """Structured detail for the selected change: CI / Queue / History."""

    def __init__(self, feed: Feed, variant: str) -> None:
        super().__init__(id="inspector")
        self.feed, self.variant = feed, variant
        self.change: Change | None = None

    @property
    def calm(self) -> bool:
        return self.variant == "calm"

    def compose(self) -> ComposeResult:
        yield Static(id="insp-head")
        with Horizontal(id="insp-actions"):
            yield Button("Open", id="open", compact=not self.calm)
            yield Button("Copy SHA", id="copy", compact=not self.calm)
        for name in ("ci", "queue", "history", "links"):
            yield Static(classes="section-title", id=f"title-{name}")
            yield Static(classes="section-body", id=f"body-{name}")

    def on_mount(self) -> None:
        if self.calm:
            self.border_title = "Inspector"
        for name, label in (("ci", "CI"), ("queue", "Queue"), ("history", "History"), ("links", "Links")):
            self.query_one(f"#title-{name}", Static).update(Content.assemble((label, "b")))

    def show(self, change: Change) -> None:
        self.change = change
        run_count = len(change.runs)
        self.query_one("#insp-head", Static).update(self.head(change))
        self.query_one("#body-ci", Static).update(self.ci(change))
        self.query_one("#body-queue", Static).update(self.queue(change))
        self.query_one("#body-history", Static).update(self.history(change))
        self.query_one("#body-links", Static).update(self.links(change))
        self.query_one("#title-ci", Static).update(
            Content.assemble(("CI", "b"), muted(f"  {run_count} run{'s' if run_count != 1 else ''}"))
        )
        self.scroll_home(animate=False)

    # -- sections --

    def head(self, change: Change) -> Content:
        meta = [change.ref]
        if change.sha:
            meta.append(change.sha[:7])
        if change.pr:
            meta.append(f"#{change.pr}")
        if change.owner:
            meta.append(f"@{change.owner}")
        return Content.assemble(
            glyph(change.state),
            " ",
            (change.title, "b"),
            "\n",
            muted(f" {SYMBOLS.dot} ".join(meta)),
            "\n",
            ci_status(change, "CI"),
            muted(f"  {SYMBOLS.arrow}  "),
            queue_status(change, "Queue", with_eta=False),
        )

    def ci(self, change: Change) -> Content:
        run = change.latest_run
        if not run:
            return Content.assemble(muted("no CI runs for this change"))
        name_width = 12 if not self.calm else 14
        bar_width = 10 if not self.calm else 14
        age = self.feed.age(run["created_at"])
        lines: list[Content] = [
            Content.assemble(
                glyph(run["state"]),
                f" {run['state']}",
                muted(f" {SYMBOLS.dot} {run['title']} #{run['id']} {SYMBOLS.dot} attempt {run['fields']['attempt']}"
                      f" {SYMBOLS.dot} {short_age(age)} ago"),
            )
        ]
        lines += self.job_tree(run, name_width, bar_width)
        if not run.get("children"):
            lines.append(dim(f"no jobs reported ({run.get('raw_state', run['state'])})"))
        return Content("\n").join(lines)

    def job_duration(self, job: dict) -> float | None:
        """Recorded duration, or elapsed time for a job that is still running."""
        if job["state"] == "running" and (started := parse_time(job.get("started_at"))):
            return (self.feed.now - started).total_seconds()
        return (job.get("fields") or {}).get("duration_seconds") or None

    def job_tree(self, run: dict, name_width: int, bar_width: int) -> list[Content]:
        jobs = run.get("children") or []
        durations = [self.job_duration(j) for j in jobs]
        scale = max((d for d in durations if d), default=1)
        lines: list[Content] = []
        for index, (job, duration) in enumerate(zip(jobs, durations)):
            last = index == len(jobs) - 1
            branch = SYMBOLS.elbow if last else SYMBOLS.tee
            style = STATE_STYLE[job["state"]]
            if duration:
                filled = max(1, round(bar_width * duration / scale))
                bar = Content.assemble(
                    (SYMBOLS.bar_full * filled, style), (SYMBOLS.bar_empty * (bar_width - filled), MUTED)
                )
                length = human_seconds(duration)
            else:  # queued / blocked: nothing to measure yet
                bar = Content.assemble((SYMBOLS.bar_empty * bar_width, MUTED))
                length = "—"
            lines.append(
                Content.assemble(
                    muted(f"{branch} "),
                    glyph(job["state"]),
                    " ",
                    fit(Content(job["title"]), name_width),
                    " ",
                    bar,
                    " ",
                    fit(Content(length), 5, "right"),
                )
            )
            # Steps are noise for healthy jobs: only open running or failed ones.
            if job["state"] in ("running", "failure"):
                trunk = "  " if last else f"{SYMBOLS.pipe} "
                for step in job.get("children") or []:
                    lines.append(
                        Content.assemble(muted(f"{trunk}   "), glyph(step["state"]), " ", (step["title"], "" if step["state"] != "success" else MUTED))
                    )
        return lines

    def queue(self, change: Change) -> Content:
        feed = self.feed
        if entry := change.entry:
            total = len(feed.entries())
            eta = eta_seconds(entry)
            lines = [
                Content.assemble(
                    glyph(entry["state"]),
                    f" {entry['state']}",
                    muted(f" {SYMBOLS.dot} #{entry['position']} of {total} {SYMBOLS.dot} {entry['raw_state'].replace('_', ' ')}"),
                ),
                dim(f"PR #{change.pr} {SYMBOLS.dot} @{entry['owner']} {SYMBOLS.dot} queued {short_age(feed.age(entry['created_at']))} ago"),
                Content.assemble(muted("ETA  "), (eta_text(eta), "b") if eta else muted("— unknown")),
            ]
            if note := next((s.note for s in feed.sources.values() if s.note and entry in s.records), ""):
                lines.append(dim(note))
            return Content("\n").join(lines)
        if change.attempts:
            holds = [a["fields"]["lock_hold_seconds"] for a in feed.attempts()]
            lines = []
            for attempt in change.attempts[:1]:
                hold = attempt["fields"]["lock_hold_seconds"]
                filled = max(1, round(14 * hold / max(holds)))
                lines += [
                    Content.assemble(glyph(attempt["state"]), f" {attempt['raw_state']}",
                                     muted(f" {SYMBOLS.dot} wait {human_seconds(attempt['fields']['lock_wait_seconds'])}")),
                    Content.assemble(
                        muted("hold "),
                        (SYMBOLS.bar_full * filled, STATE_STYLE[attempt["state"]]),
                        (SYMBOLS.bar_empty * (14 - filled), MUTED),
                        f" {human_seconds(hold)}",
                    ),
                ]
            return Content("\n").join(lines)
        return Content.assemble(muted("not in the merge queue"))

    def history(self, change: Change) -> Content:
        """Newest-first timeline across every source."""
        events: list[tuple[datetime, Content]] = []
        for run in change.runs:
            when = parse_time(run.get("finished_at") or run["updated_at"] or run["created_at"])
            events.append((when, Content.assemble(glyph(run["state"]), f" CI #{run['id']} {run['state']}", muted(f" {ci_detail(run)}"))))
        if change.entry:
            events.append((parse_time(change.entry["created_at"]),
                           Content.assemble(glyph("queued"), f" queued at #{change.entry['position']}")))
        for attempt in change.attempts:
            events.append((parse_time(attempt["finished_at"]),
                           Content.assemble(glyph(attempt["state"]), f" merge attempt {attempt['raw_state']}")))
        events.sort(key=lambda e: e[0], reverse=True)
        lines = [
            Content.assemble(fit(Content.assemble(muted(f"{short_age(self.feed.age(when))} ago")), 8), text)
            for when, text in events[:6]
        ]
        return Content("\n").join(lines) if lines else dim("no events")

    def links(self, change: Change) -> Content:
        rows = [("run", change.latest_run), ("queue", change.entry)]
        lines = [
            Content.assemble(fit(dim(label), 6), record["url"])
            for label, record in rows
            if record and record.get("url")
        ]
        return Content("\n").join(lines) if lines else dim("no links")

    @on(Button.Pressed)
    def _pressed(self, event: Button.Pressed) -> None:
        action = self.app.action_open if event.button.id == "open" else self.app.action_copy_sha
        action()


# --- per-source tables (secondary tabs) ------------------------------------ #


def rich_style(app: App, token: str, bold: bool = False) -> RichStyle:
    """Resolve a theme variable ('$success') for Rich-rendered widgets such as DataTable."""
    variables = app.get_css_variables()
    value = variables[token.lstrip("$")]
    if value.startswith("auto"):  # e.g. $text-muted: "auto 60%" = foreground at 60% over the surface
        alpha = float(value.split()[1].rstrip("%")) / 100
        color = Color.parse(variables["foreground"]).blend(Color.parse(variables["surface"]), 1 - alpha)
    else:
        color = Color.parse(value)
    return RichStyle(color=color.rich_color, bold=bold)


class SourceTable(DataTable):
    """The classic per-source table: every record of one source, one row each."""

    def __init__(self, feed: Feed, source: Source) -> None:
        super().__init__(cursor_type="row", zebra_stripes=False, id=f"table-{source.key}", classes="source-table")
        self.feed, self.source = feed, source
        self.refs: dict[str, str] = {}  # row key -> change ref

    def on_mount(self) -> None:
        self.populate()
        self.app.theme_changed_signal.subscribe(self, lambda _theme: self.populate())

    def cell(self, text: str, token: str = "", bold: bool = False) -> Text:
        return Text(text, style=rich_style(self.app, token, bold) if token else RichStyle())

    def state_cell(self, state: str) -> Text:
        return self.cell(f"{SYMBOLS.glyphs[state]} {state}", STATE_STYLE[state])

    def populate(self) -> None:
        self.clear(columns=True)
        self.refs.clear()
        kinds = {r["kind"] for r in self.source.records}
        if "run" in kinds:
            self.add_columns("State", "Run", "Workflow", "Branch", "SHA", "Jobs", "Age")
            for record in self.source.records:
                jobs = Text()
                for job in record.get("children") or []:
                    jobs.append(SYMBOLS.glyphs[job["state"]], style=rich_style(self.app, STATE_STYLE[job["state"]]))
                self.add_row(
                    self.state_cell(record["state"]),
                    self.cell(f"#{record['id']}"),
                    self.cell(record["title"]),
                    self.cell(record["ref"]),
                    self.cell((record.get("sha") or "")[:7], "$text-muted"),
                    jobs,
                    self.cell(short_age(self.feed.age(record["created_at"])), "$text-muted"),
                    key=record["id"],
                )
                self.refs[record["id"]] = record["ref"]
        else:
            self.add_columns("#", "State", "Change", "Owner", "Raw", "ETA / hold", "Age")
            for record in self.source.records:
                fields = record.get("fields") or {}
                if record["kind"] == "queue_entry":
                    note = eta_text(fields["estimated_seconds"]) if fields.get("estimated_seconds") else "—"
                    place, when = str(record["position"]), record["created_at"]
                else:
                    note = human_seconds(fields["lock_hold_seconds"]) + " hold"
                    place, when = SYMBOLS.dot, record["finished_at"]
                self.add_row(
                    self.cell(place, "$text-muted"),
                    self.state_cell(record["state"]),
                    self.cell(record["title"]),
                    self.cell(record.get("owner") or "", "$text-muted"),
                    self.cell(record["raw_state"], "$text-muted"),
                    self.cell(note),
                    self.cell(short_age(self.feed.age(when)), "$text-muted"),
                    key=record["id"],
                )
                self.refs[record["id"]] = record["ref"]

    @on(DataTable.RowHighlighted)
    def _highlighted(self, event: DataTable.RowHighlighted) -> None:
        if self.has_focus and event.row_key.value in self.refs:
            self.post_message(ChangeSelected(self.refs[event.row_key.value]))


# --------------------------------------------------------------------------- #
# Variant A: dense                                                             #
# --------------------------------------------------------------------------- #


@dataclass
class Column:
    key: str
    label: str
    width: int  # 0 = takes the remaining space
    align: str = "left"


def dense_columns(width: int) -> list[Column]:
    """Pick columns for a table `width` cells wide; narrow tables keep only the essentials."""
    cols = [Column("glyph", "", 1), Column("ref", "CHANGE", 16 if width < 90 else 18)]
    wide = width >= 96
    if wide:
        cols += [Column("pr", "PR", 5), Column("title", "TITLE", 0), Column("owner", "OWNER", 6)]
    cols += [
        Column("ci", "CI", 16),
        Column("queue", "QUEUE", 16 if not wide else 24),
        Column("age", "AGE", 4, "right"),
    ]
    fixed = sum(c.width for c in cols) + (len(cols) - 1)
    flex = width - fixed
    if flex < 12:  # not enough room for the flexible title column
        cols = [c for c in cols if c.key not in ("title", "owner")]
        flex = 0
    for col in cols:
        if col.width == 0:
            col.width = flex
    return cols


def dense_cell(col: Column, change: Change, feed: Feed, compact: bool) -> Content:
    match col.key:
        case "glyph":
            return Content.assemble(glyph(change.state))
        case "ref":
            return Content.assemble((change.ref, "b"))
        case "pr":
            return Content.assemble(muted(f"#{change.pr}" if change.pr else ""))
        case "title":
            return Content(change.title)
        case "owner":
            return Content.assemble(muted(change.owner or ""))
        case "ci":
            return ci_status(change)
        case "queue":
            content = queue_status(change, "Q" if compact else "Queue")
            return Content.assemble(muted(f"{SYMBOLS.arrow} "), content)
        case "age":
            return Content.assemble(muted(short_age(feed.age(change.activity))))
    return Content("")


class ChangeTable(Vertical):
    """Dense changes table: header line + OptionList with group rows (disabled, so the cursor skips them)."""

    def __init__(self, feed: Feed) -> None:
        super().__init__(classes="change-list")
        self.feed = feed
        self.width_built = 0

    def compose(self) -> ComposeResult:
        yield Static(id="colhead")
        yield OptionList(id="rows")

    def on_resize(self) -> None:
        # OptionList pads each option by 1 cell either side and reserves a scrollbar.
        width = self.size.width - 2 - 1
        if width != self.width_built and width > 20:
            self.width_built = width
            self.rebuild()

    def rebuild(self) -> None:
        width = self.width_built
        cols = dense_columns(width)
        compact = width < 96
        head = Content("").join(
            fit(Content.assemble(muted(c.label)), c.width, c.align).append(" ") for c in cols
        )
        self.query_one("#colhead", Static).update(head)
        options: list[Option] = []
        for key, name, state, changes in self.feed.grouped():
            if not changes:
                continue
            title = Content.assemble(glyph(state), " ", (name.upper(), f"b {STATE_STYLE[state]}"), muted(f"  {len(changes)} "))
            rule = SYMBOLS.rule * max(0, width - title.cell_length)
            options.append(Option(Content.assemble(title, muted(rule)), id=f"group-{key}", disabled=True))
            for change in changes:
                row = Content("").join(fit(dense_cell(c, change, self.feed, compact), c.width, c.align).append(" ") for c in cols)
                options.append(Option(row, id=change.ref))
        rows = self.query_one("#rows", OptionList)
        rows.clear_options()
        rows.add_options(options)
        self.highlight(self.app.selected)

    def highlight(self, ref: str | None) -> None:
        rows = self.query_one("#rows", OptionList)
        if ref:
            with suppress(OptionDoesNotExist):  # rows are built on first resize
                rows.highlighted = rows.get_option_index(ref)

    @on(OptionList.OptionHighlighted)
    def _highlighted(self, event: OptionList.OptionHighlighted) -> None:
        event.stop()
        if event.option_id and not event.option_id.startswith("group-"):
            self.post_message(ChangeSelected(event.option_id))


# --------------------------------------------------------------------------- #
# Variant B: calm                                                              #
# --------------------------------------------------------------------------- #


def pipeline(change: Change) -> Content:
    """CI → Queue → Merge, glyph per stage; detail only on the first stage that is not done."""
    run, entry = change.latest_run, change.entry
    attempt = change.attempts[0] if change.attempts else None
    merge_state = (
        "running" if entry and entry["state"] == "running" else attempt["state"] if attempt else "neutral"
    )
    if entry and entry["state"] != "running":
        merge_state = "queued"
    stages = [
        ("CI", run["state"] if run else "neutral", ci_detail(run) if run else ""),
        ("Queue", (entry or attempt or {}).get("state", "neutral"),
         f"#{entry['position']} {entry['state']}" if entry else (attempt["raw_state"] if attempt else "")),
        ("Merge", merge_state, ""),
    ]
    focus = next((i for i, (_, state, _) in enumerate(stages) if state != "success"), None)
    parts: list[Any] = []
    for index, (label, state, detail) in enumerate(stages):
        if index:
            parts.append(muted(f"  {SYMBOLS.arrow}  "))
        parts += [(label, "" if state != "neutral" else MUTED), " ", glyph(state)]
        if index == focus and detail:
            parts.append(f" {detail}")
    return Content.assemble(*parts)


class ChangeCard(Vertical):
    """One change: title + meta on top, compact pipeline underneath."""

    def __init__(self, change: Change, feed: Feed) -> None:
        super().__init__(classes="card")
        self.change, self.feed = change, feed

    def compose(self) -> ComposeResult:
        change = self.change
        meta = [f"#{change.pr}"] if change.pr else []
        if change.owner:
            meta.append(f"@{change.owner}")
        meta.append(short_age(self.feed.age(change.activity)))
        with Horizontal(classes="card-top"):
            yield Static(Content.assemble(glyph(change.state), "  ", (change.title, "b")), classes="card-title")
            yield Static(dim(f" {SYMBOLS.dot} ".join(meta)), classes="card-meta")
        yield Static(Content.assemble("   ", pipeline(change)), classes="card-pipe")

    def on_click(self) -> None:
        self.post_message(ChangeSelected(self.change.ref))


class CardList(VerticalScroll, can_focus=True):
    """Cards grouped under quiet headings. Up/down (or j/k) move the selection."""

    BINDINGS = [
        Binding("down,j", "move(1)", "Next", show=False),
        Binding("up,k", "move(-1)", "Previous", show=False),
    ]

    def __init__(self, feed: Feed) -> None:
        super().__init__(classes="change-list")
        self.feed = feed
        self.cards: dict[str, ChangeCard] = {}

    def compose(self) -> ComposeResult:
        counts = self.feed.counts()
        for key, name, state, changes in self.feed.grouped():
            if not changes:
                continue
            yield Static(
                Content.assemble(glyph(state), "  ", (name, f"b {STATE_STYLE[state]}"), muted(f"  {counts[key]}")),
                classes="group-head",
            )
            for change in changes:
                card = ChangeCard(change, self.feed)
                self.cards[change.ref] = card
                yield card

    def highlight(self, ref: str | None) -> None:
        for key, card in self.cards.items():
            card.set_class(key == ref, "-selected")
        if ref in self.cards:
            self.scroll_to_widget(self.cards[ref], animate=False)

    def action_move(self, step: int) -> None:
        refs = list(self.cards)
        current = refs.index(self.app.selected) if self.app.selected in refs else 0
        self.post_message(ChangeSelected(refs[(current + step) % len(refs)]))

    def on_change_selected(self, message: ChangeSelected) -> None:
        self.focus()  # make arrow keys work right after a click


# --------------------------------------------------------------------------- #
# Apps                                                                         #
# --------------------------------------------------------------------------- #

# Shared widget styling. Only theme variables: switch themes and the whole look follows.
BASE_CSS = """
#status { height: 1; padding: 0 1; background: $panel; color: $foreground; }
#status #brand { width: auto; margin-right: 2; color: $primary; }
#status .pill { width: auto; padding: 0 1; margin-right: 1; }
#status .pill.live { background: $success 25%; color: $text-success; }
#status .pill.stale { background: $warning 30%; color: $text-warning; }
#status #counts-long, #status #counts-short { width: 1fr; text-align: right; }
#status #counts-short { display: none; }
.-narrow #status #counts-long { display: none; }
.-narrow #status #counts-short { display: block; }

#queue { height: auto; }
#queue-head { height: 1; }
#queue-head #queue-title { width: auto; }
#queue-head #queue-eta { width: 1fr; min-width: 18; padding-left: 2; }
#queue-head #trend-label { width: auto; margin-left: 2; }
.-narrow #queue-head #trend-label { display: none; }
#queue-head #trend { margin: 0 0 0 1; }
#queue-head #trend-stats { width: auto; }
Sparkline > .sparkline--max-color { color: $error; }
Sparkline > .sparkline--min-color { color: $success; }
#chain { height: auto; }
#chain .link { width: 3; text-align: center; color: $text-muted; }
#chain .chip { width: 1fr; text-wrap: nowrap; text-overflow: ellipsis; }

#body { height: 1fr; }
TabbedContent { height: 1fr; }
TabbedContent > ContentSwitcher { height: 1fr; }
TabPane { padding: 0; }
.change-list { height: 1fr; }
.source-table { height: 1fr; }
DataTable > .datatable--header { background: $panel; color: $text-muted; text-style: bold; }
DataTable > .datatable--cursor { background: $primary 30%; color: $foreground; }

#inspector { height: 1fr; }
#inspector #insp-head { height: auto; }
#inspector #insp-actions { height: auto; }
#inspector .section-body { height: auto; text-wrap: nowrap; text-overflow: ellipsis; }
#inspector Button { margin-right: 1; }
"""


class MockupApp(App):
    """Shared behaviour: theme, selection, the two actions, tab switching."""

    variant = ""
    TITLE = "queue-watch"
    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("o", "open", "Open"),
        Binding("c", "copy_sha", "Copy SHA"),
        Binding("1", "tab(0)", "Changes"),
        Binding("2", "tab(1)", "CI"),
        Binding("3", "tab(2)", "Queue"),
    ]

    def __init__(self, feed: Feed, *, theme: str, ascii_mode: bool, select: str | None = None) -> None:
        super().__init__()
        self.feed = feed
        self.theme_name = theme
        self.selected: str | None = None
        self.initial = select or feed.grouped()[0][3][0].ref
        self.set_class(ascii_mode, "-ascii")

    def on_mount(self) -> None:
        self.theme = self.theme_name
        self.select(self.initial)

    def on_resize(self, event: Resize) -> None:
        # Layout tiers: the CSS keys off these two classes ("-narrow" / "-short").
        self.set_class(event.size.width < 120, "-narrow")
        self.set_class(event.size.height < 36, "-short")

    # -- composition pieces shared by both variants --

    def tabs(self, changes_view: Any) -> ComposeResult:
        """Changes first (the default), then one secondary tab per source."""
        feed = self.feed
        with TabbedContent(initial="tab-changes", id="tabs"):
            with TabPane(f"Changes {len(feed.changes)}", id="tab-changes"):
                yield changes_view
            for source in feed.sources.values():
                with TabPane(f"{source.label} {len(source.records)}", id=f"tab-{source.key}"):
                    yield SourceTable(feed, source)

    # -- selection --

    def select(self, ref: str) -> None:
        if ref == self.selected:
            return
        self.selected = ref
        self.query_one(".change-list").highlight(ref)
        self.query_one(Inspector).show(self.feed.change(ref))

    def on_change_selected(self, message: ChangeSelected) -> None:
        self.select(message.ref)

    # -- actions (visual only: the real app is read-only too) --

    def action_open(self) -> None:
        change = self.feed.change(self.selected)
        self.notify(change.url or "no URL for this change", title="Open")

    def action_copy_sha(self) -> None:
        change = self.feed.change(self.selected)
        if change.sha:
            self.copy_to_clipboard(change.sha)
        self.notify(change.sha or "no SHA", title="Copied SHA")

    def action_tab(self, index: int) -> None:
        panes = [pane.id for pane in self.query(TabPane)]
        if index < len(panes):
            self.query_one(TabbedContent).active = panes[index]


class DenseApp(MockupApp):
    """Control room: everything visible at once, tight rows, inspector in a right column."""

    variant = "dense"
    CSS = (
        BASE_CSS
        + """
    Screen { background: $surface; }
    #status { background: $panel; }
    #queue { padding: 0 1; border-bottom: solid $foreground 20%; background: $surface; }
    #queue-head { color: $foreground; }
    .chip { height: 1; background: $panel; color: $foreground; }
    .chip.holder { background: $primary; color: $text; text-style: bold; }
    Tabs { height: 2; }
    TabbedContent { width: 1fr; }
    #inspector { width: 32%; min-width: 40; max-width: 62; border-left: solid $foreground 20%; padding: 0 1; background: $surface; }
    #insp-head { padding-bottom: 1; }
    #insp-actions { padding-bottom: 1; }
    .section-title { background: $panel; padding: 0 1; margin-top: 1; height: 1; }
    .section-body { padding: 0 1; }
    #colhead { height: 1; padding: 0 1; background: $panel; }
    OptionList { background: $surface; border: none; padding: 0; }
    OptionList > .option-list--option { padding: 0 1; }
    OptionList > .option-list--option-highlighted { background: $primary 28%; color: $foreground; text-style: none; }
    OptionList:focus > .option-list--option-highlighted { background: $primary 40%; color: $foreground; text-style: none; }
    OptionList > .option-list--option-hover { background: $primary 12%; }
    OptionList > .option-list--option-disabled { color: $text-muted; }
    #inspector Button { background: $primary 30%; color: $foreground; }
    """
    )

    def compose(self) -> ComposeResult:
        yield StatusBar(self.feed)
        yield QueueStrip(self.feed, "dense")
        with Horizontal(id="body"):
            yield from self.tabs(ChangeTable(self.feed))
            yield Inspector(self.feed, "dense")
        yield Footer()


class CalmApp(MockupApp):
    """Calm: one card per change, generous spacing, inspector as a rounded side drawer."""

    variant = "calm"
    CSS = (
        BASE_CSS
        + """
    Screen, #queue, #chain, #body, TabbedContent, TabPane, CardList { background: $background; }
    #status { height: 1; background: $background; padding: 0 2; }
    #status .pill { border: none; }
    #queue { padding: 1 2 0 2; }
    #queue-head { margin-bottom: 1; }
    #chain .link { height: 4; content-align: center middle; }
    .chip { height: 4; border: round $foreground 30%; padding: 0 1; background: $surface; margin: 0; }
    .chip.holder { border: heavy $primary; background: $primary 14%; }
    .-ascii .chip { border: ascii $foreground 40%; }
    .-ascii .chip.holder { border: ascii $primary; }
    .-ascii .card { border: ascii $foreground 40%; }
    .-ascii #inspector { border: ascii $foreground 40%; }
    Tabs { height: 2; margin: 1 2 0 2; }
    #body { padding: 0 2 0 1; }
    TabbedContent { width: 1fr; }
    CardList { padding: 0 1 0 1; scrollbar-size-vertical: 1; }
    .group-head { margin: 1 0 0 1; height: 1; }
    .card { height: 4; border: round $foreground 20%; padding: 0 2; margin: 0 0 1 0; background: $surface; }
    .card:hover { border: round $primary 50%; }
    .card.-selected { border: round $primary; background: $primary 12%; }
    .card-top { height: 1; }
    .card-title { width: 1fr; padding-right: 2; text-wrap: nowrap; text-overflow: ellipsis; }
    .card-meta { width: auto; }
    .card-pipe { height: 1; text-wrap: nowrap; text-overflow: ellipsis; }
    #inspector { width: 38%; min-width: 40; max-width: 64; margin: 1 0 0 2; padding: 1 2; border: round $foreground 25%; background: $surface; }
    #inspector #insp-head { padding-bottom: 1; }
    #inspector #insp-actions { padding-bottom: 1; }
    .section-title { color: $primary; margin-top: 1; height: 1; border-bottom: none; }
    .section-body { padding-left: 0; }
    #title-links, #body-links { display: none; }
    #inspector Button { background: $primary 25%; color: $foreground; border: round $primary 60%; min-width: 12; }
    #inspector Button:hover { background: $primary 45%; }
    .-short #queue { padding: 0 2; }
    .-short #queue-head { margin-bottom: 0; }
    .-short Tabs { margin-top: 0; }
    .-short #inspector { margin-top: 0; padding: 0 1; }
    .-short #inspector Button { border: none; height: 1; min-width: 10; }
    """
    )

    def compose(self) -> ComposeResult:
        yield StatusBar(self.feed)
        yield QueueStrip(self.feed, "calm")
        with Horizontal(id="body"):
            yield from self.tabs(CardList(self.feed))
            yield Inspector(self.feed, "calm")
        yield Footer()


# --------------------------------------------------------------------------- #
# CLI                                                                          #
# --------------------------------------------------------------------------- #


def parse_size(text: str) -> tuple[int, int]:
    match = re.fullmatch(r"(\d+)x(\d+)", text)
    if not match:
        raise argparse.ArgumentTypeError("expected COLSxROWS, e.g. 160x45")
    return int(match.group(1)), int(match.group(2))


def parse_lag(text: str) -> tuple[str, int]:
    source, _, seconds = text.partition("=")
    if not seconds.isdigit():
        raise argparse.ArgumentTypeError("expected SOURCE=SECONDS, e.g. merge-queue=95")
    return source, int(seconds)


def build_parser() -> argparse.ArgumentParser:
    themes = sorted(App().available_themes)
    parser = argparse.ArgumentParser(description="queue-watch design mockups (static, fixture-driven).")
    parser.add_argument("--variant", choices=("dense", "calm"), required=True)
    parser.add_argument("--theme", default="tokyo-night", choices=themes, metavar="NAME",
                        help="any built-in Textual theme: " + ", ".join(themes))
    parser.add_argument("--ascii", action="store_true", help="ASCII glyphs instead of Unicode")
    parser.add_argument("--screenshot", type=Path, metavar="OUT.svg", help="render headlessly to an SVG and exit")
    parser.add_argument("--size", type=parse_size, default=(160, 45), metavar="COLSxROWS",
                        help="terminal size for --screenshot (default 160x45)")
    parser.add_argument("--fixture", type=Path, default=DEFAULT_FIXTURE)
    parser.add_argument("--lag", type=parse_lag, action="append", metavar="SOURCE=SECONDS",
                        help="simulated seconds since a source's last poll (default ci=4, merge-queue=95)")
    parser.add_argument("--select", metavar="REF", help="change to inspect first (default: top of the list)")
    return parser


def make_app(args: argparse.Namespace) -> MockupApp:
    global SYMBOLS
    SYMBOLS = ASCII if args.ascii else UNICODE
    feed = load_feed(args.fixture, {**DEFAULT_LAG, **dict(args.lag or [])})
    app_class = DenseApp if args.variant == "dense" else CalmApp
    return app_class(feed, theme=args.theme, ascii_mode=args.ascii, select=args.select)


async def screenshot(app: MockupApp, out: Path, size: tuple[int, int]) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    async with app.run_test(size=size) as pilot:
        await pilot.pause()
        await pilot.pause(0.2)  # let the width-dependent rebuilds settle
        app.save_screenshot(filename=out.name, path=str(out.parent))


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.screenshot:
        os.environ.pop("NO_COLOR", None)  # Textual would render the SVG in greyscale
    app = make_app(args)
    if args.screenshot:
        asyncio.run(screenshot(app, args.screenshot, args.size))
        print(args.screenshot)
    else:
        app.run()  # mouse is on by default: click rows, cards, chips and tabs
    return 0


if __name__ == "__main__":
    sys.exit(main())
