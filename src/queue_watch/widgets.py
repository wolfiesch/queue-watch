"""Widgets and text helpers for the dashboard (see design/README.md).

Every colour is a Textual theme variable, so ``--theme`` and the command palette
restyle everything. States always carry a glyph as well as a colour, and
``use_ascii`` swaps the glyphs for plain ASCII.
"""

from __future__ import annotations

import statistics
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from itertools import pairwise
from typing import Any
from zoneinfo import ZoneInfo

from rich.style import Style as RichStyle
from rich.text import Text
from textual import on
from textual.app import App, ComposeResult
from textual.color import Color
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.content import Content
from textual.message import Message
from textual.widgets import Button, OptionList, Sparkline, Static
from textual.widgets.option_list import Option, OptionDoesNotExist

from .changes import GROUPS, Change, grouped, stamp
from .process import clean

PACIFIC = ZoneInfo("America/Los_Angeles")
MUTED = "$text-muted"
STATE_STYLE = {
    "running": "$primary",
    "queued": MUTED,
    "success": "$success",
    "failure": "$error",
    "cancelled": MUTED,
    "blocked": "$warning",
    "neutral": MUTED,
    "unknown": MUTED,
}


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
    dash: str
    live: str
    folded: str
    unfolded: str


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
    dash="—",
    live="✓",
    folded="▸",
    unfolded="▾",
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
    dash="-",
    live="+",
    folded=">",
    unfolded="v",
)
SYMBOLS = UNICODE


def use_ascii() -> None:
    global SYMBOLS
    SYMBOLS = ASCII


# --- text helpers ---------------------------------------------------------- #


def glyph(state: str) -> tuple[str, str]:
    """(glyph, style) for a normalized state, for ``Content.assemble``."""
    return SYMBOLS.glyphs.get(state, SYMBOLS.glyphs["unknown"]), STATE_STYLE.get(state, MUTED)


def muted(text: str) -> tuple[str, str]:
    return text, MUTED


def dim(text: str) -> Content:
    return Content.assemble(muted(text))


def raw(record: dict) -> str:
    return clean(record.get("raw_state") or record["state"]).replace("_", " ")


def fit(content: Content, width: int, align: str = "left") -> Content:
    content = content.truncate(width, ellipsis=True)
    gap = width - content.cell_length
    if align == "right" and gap > 0:
        return content.pad_left(gap)
    return content.pad_right(max(gap, 0))


def lines(rows: list[Content]) -> Content:
    return Content("\n").join(rows)


def human_seconds(seconds: float) -> str:
    seconds = max(0, round(seconds))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        minutes, rest = divmod(seconds, 60)
        return f"{minutes}m{rest:02d}s" if rest else f"{minutes}m"
    hours, rest = divmod(seconds, 3600)
    return f"{hours}h{rest // 60:02d}m"


def duration(value: object) -> str:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return "-"
    return human_seconds(value)


def eta_text(seconds: float) -> str:
    """Estimates are rough: whole minutes past a minute and a half, ``~7m``."""
    return f"~{human_seconds(seconds if seconds < 90 else round(seconds / 60) * 60)}"


def short_age(seconds: float | None) -> str:
    """Coarse age for table cells: 45s, 12m, 3h, 2d."""
    if seconds is None:
        return "?"
    seconds = max(0, round(seconds))
    for size, unit in ((86400, "d"), (3600, "h"), (60, "m")):
        if seconds >= size:
            return f"{seconds // size}{unit}"
    return f"{seconds}s"


def now() -> float:
    """Wall-clock epoch seconds; the one place ages read the clock (tests replace it)."""
    return time.time()


def ago(value: str | float | None) -> float | None:
    """Seconds since an ISO timestamp or epoch seconds."""
    if isinstance(value, (int, float)):
        return now() - value if value else None
    try:
        return now() - datetime.fromisoformat(value).timestamp()
    except (TypeError, ValueError):
        return None


def clock(value: str | None) -> str:
    try:
        return datetime.fromisoformat(value).astimezone(PACIFIC).strftime("%-I:%M:%S %p %Z")
    except (TypeError, ValueError):
        return "unknown"


def elapsed(record: dict) -> float | None:
    """How long a job or attempt took, or has taken so far while running."""
    fields = record.get("fields") or {}
    if isinstance(fields.get("duration_seconds"), (int, float)):
        return fields["duration_seconds"]
    start = stamp(record, "started_at")
    if not start:
        return None
    if record["state"] == "running":
        return now() - start
    end = stamp(record, "finished_at")
    return end - start if end >= start else None


def eta_of(record: dict | None) -> float | None:
    value = ((record or {}).get("fields") or {}).get("estimated_seconds")
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def rich_style(app: App, token: str, bold: bool = False) -> RichStyle:
    """Resolve a theme variable ('$success') for Rich-rendered widgets such as DataTable."""
    variables = app.get_css_variables()
    value = variables.get(token.lstrip("$"), variables["foreground"])
    if value.startswith("auto"):  # "auto 60%": foreground at 60% over the surface
        alpha = float(value.split()[1].rstrip("%")) / 100
        color = Color.parse(variables["foreground"]).blend(
            Color.parse(variables["surface"]), 1 - alpha
        )
    else:
        color = Color.parse(value)
    return RichStyle(color=color.rich_color, bold=bold)


def state_text(app: App, record: dict) -> Text:
    """Glyph plus the adapter's own state word, for DataTable cells."""
    text, style = glyph(record["state"])
    return Text(f"{text} {raw(record)}", style=rich_style(app, style))


# --- change summaries ------------------------------------------------------ #


def ci_status(change: Change, label: str = "CI") -> Content:
    """``CI ✗ test`` (the worst workflow) or ``CI ✓ 3 runs``."""
    checks = change.checks
    if not checks:
        return Content.assemble(muted(f"{label} {SYMBOLS.dash}"))
    state = change.ci_state
    if state == "success" and len(checks) > 1:
        detail = f"{len(checks)} runs"
    else:
        worst = next(hit.record for hit in checks if hit.record["state"] == state)
        detail = clean(worst["title"]) if len(checks) > 1 else raw(worst)
    return Content.assemble(muted(f"{label} "), glyph(state), " ", detail)


def queue_status(change: Change, label: str = "Queue", with_eta: bool = True) -> Content:
    """``Queue ! #4 unmergeable ~7m`` or, after an attempt, ``Queue ✓ merged 38s``."""
    if change.entry:
        entry = change.entry.record
        parts: list[Any] = [muted(f"{label} "), glyph(entry["state"])]
        if isinstance(entry.get("position"), int):
            parts.append(f" #{entry['position']}")
        parts.append(f" {raw(entry)}")
        if with_eta and (eta := eta_of(entry)):
            parts.append(muted(f" {eta_text(eta)}"))
        return Content.assemble(*parts)
    if change.attempts:
        attempt = change.attempts[0].record
        parts = [muted(f"{label} "), glyph(attempt["state"]), f" {raw(attempt)}"]
        if with_eta and (took := elapsed(attempt)):
            parts.append(muted(f" {human_seconds(took)}"))
        return Content.assemble(*parts)
    return Content.assemble(muted(f"{label} {SYMBOLS.dash}"))


# --- messages -------------------------------------------------------------- #


class ChangeSelected(Message):
    def __init__(self, key: str) -> None:
        super().__init__()
        self.key = key


class ChipClicked(Message):
    def __init__(self, source: str, record_id: str) -> None:
        super().__init__()
        self.source, self.record_id = source, record_id


# --- status bar ------------------------------------------------------------ #


@dataclass
class Health:
    label: str
    word: str  # live, stale, paused, waiting, unavailable
    age: float | None


class StatusBar(Horizontal):
    """One line: brand, a pill per source (label, state, age), group counts."""

    def __init__(self, count: int) -> None:
        super().__init__(id="status")
        self.count = count

    def compose(self) -> ComposeResult:
        yield Static(Content.assemble(("queue-watch", "b")), id="brand")
        for index in range(self.count):
            yield Static(classes="pill", id=f"pill-{index}")
        yield Static(id="counts-long", classes="counts")
        yield Static(id="counts-short", classes="counts")

    def show(self, health: list[Health], counts: dict[str, int] | None) -> None:
        """``health`` holds one entry per source, in configuration order."""
        for index, item in enumerate(health):
            pill = self.query_one(f"#pill-{index}", Static)
            mark = SYMBOLS.live if item.word in ("live", "paused") else "!"
            text = f"{item.word} {human_seconds(item.age)}" if item.age is not None else item.word
            pill.update(Content.assemble((mark, "b"), f" {clean(item.label)} ", (text, "b")))
            pill.set_classes(f"pill {item.word}")
        for name, words in (("long", True), ("short", False)):
            parts: list[Any] = []
            if counts is not None:
                for key, _, state in GROUPS:
                    parts += [glyph(state), f" {counts[key]} {key}" if words else f" {counts[key]}"]
                    parts.append("   " if words else "  ")
            self.query_one(f"#counts-{name}", Static).update(Content.assemble(*parts[:-1]))


# --- queue strip ----------------------------------------------------------- #

CHIP_MIN_WIDTH = 26


def chain_order(records: list[dict]) -> list[dict]:
    """Queue order: active holders without a position first, then by position."""

    def key(record: dict) -> tuple[int, float]:
        position = record.get("position")
        if isinstance(position, int):
            return (1, position)
        return (0 if record["state"] == "running" else 2, 0)

    return sorted(records, key=key)


class QueueChip(Static):
    """One queue entry: bordered, with its position in the border title.

    On short terminals (the app's ``-short`` class) it is one borderless line instead.
    """

    def __init__(self, source: str, record: dict, change: Change | None, holder: bool) -> None:
        super().__init__(classes="chip holder" if holder else "chip")
        self.source, self.record, self.change, self.holder = source, record, change, holder
        self.full = self.line = Content("")

    def on_mount(self) -> None:
        position = self.record.get("position")
        label = str(position) if isinstance(position, int) else raw(self.record)
        label = f"{label} {SYMBOLS.dot} next" if self.holder else label
        # Strings assigned to border_title and tooltip parse as markup; Content does not.
        self.border_title = Content(label)
        title = clean(self.record["title"])
        if self.change and self.change.pr and not title.startswith("#"):
            title = f"#{self.change.pr} {title}"
        eta = eta_of(self.record)
        tail: list[Any] = [muted(f" {SYMBOLS.dot} "), (eta_text(eta), "b")] if eta else []
        if not eta and self.record.get("owner"):
            tail = [muted(f" {SYMBOLS.dot} {clean(self.record['owner'])}")]
        state = glyph(self.record["state"])
        self.full = Content.assemble((title, "b"), "\n", state, f" {raw(self.record)}", *tail)
        self.line = Content.assemble(muted(f"{label} "), state, " ", (title, "b"), *tail)
        detail = [title]
        if (name := self.record.get("subtitle")) and name != self.record["title"]:
            detail.append(clean(name))
        self.tooltip = Content("\n".join([*detail, raw(self.record)]))
        self.refresh()

    def render(self) -> Content:
        return self.line if self.app.has_class("-short") else self.full

    def on_click(self) -> None:
        self.post_message(ChipClicked(self.source, self.record["id"]))


DEPTH_SAMPLES = 120  # one per fetch: half an hour at the default 15 s interval
SPARK_CELLS = 20
SPARK_MIN = 4  # narrower than this a sparkline shows no trend: drop it instead


class QueueStrip(Vertical):
    """Header (count, ETA, depth and attempt trends) over a FIFO chain of chips for one source."""

    def __init__(self, source: str, label: str) -> None:
        super().__init__(classes="queue")
        self.source, self.label = source, label
        self.border_title = Content(clean(label))
        self.entries: list[dict] = []
        self.attempts: list[dict] = []  # with a duration: the "took" trend
        self.merges: list[float] = []  # finish times of every successful attempt
        self.history = False
        self.by_record: dict[str, Change] = {}
        self.built: tuple | None = None
        # Queue length after each fetch this session; sources keep no history of it.
        self.depth: deque[int] = deque(maxlen=DEPTH_SAMPLES)
        # Cell widths of the head row's text, for fit(): title, eta, rate, stats.
        self.text_cells = (0, 0, 0, 0)

    def compose(self) -> ComposeResult:
        with Horizontal(classes="queue-head"):
            yield Static(classes="queue-title")
            yield Static(classes="queue-eta")
            yield Static(dim("depth"), classes="depth spark-label")
            yield Sparkline([], classes="depth spark")
            yield Static(classes="rate")
            yield Static(dim("took"), classes="took spark-label")
            yield Sparkline([], classes="took spark")
            yield Static(classes="took took-stats")
        yield Horizontal(classes="chain")

    def sample(self, records: list[dict]) -> None:
        """Record the queue length of one fresh snapshot."""
        self.depth.append(sum(r["kind"] == "queue_entry" for r in records))

    async def show(self, records: list[dict], by_record: dict[str, Change]) -> None:
        self.entries = chain_order([r for r in records if r["kind"] == "queue_entry"])
        self.attempts = sorted(
            (r for r in records if r["kind"] == "attempt" and elapsed(r) is not None),
            key=lambda r: stamp(r, "finished_at", "started_at"),
        )
        # Merge counts need only a finish time; a log may omit start times and durations.
        finished = [r for r in records if r["kind"] == "attempt" and stamp(r, "finished_at")]
        self.history = bool(finished)
        self.merges = [stamp(r, "finished_at") for r in finished if r["state"] == "success"]
        self.by_record = by_record
        self.head()
        await self.chain()

    def head(self) -> None:
        waiting = len(self.entries)
        status = f"{waiting} waiting"
        if not waiting:
            status = "nothing waiting"
            if self.merges:
                last = short_age(now() - max(self.merges))
                status += f" {SYMBOLS.dot} last merge {last} ago"
        title = Content.assemble((status, "b"))
        self.query_one(".queue-title", Static).update(title)
        clears = max((eta_of(e) or 0 for e in self.entries), default=0)
        eta = (
            Content.assemble(muted("clears in "), (eta_text(clears), "b")) if clears else Content()
        )
        self.query_one(".queue-eta", Static).update(eta)
        if len(self.depth) > 1:
            self.query_one(".depth.spark", Sparkline).data = list(self.depth)
        merged = sum(now() - finished <= 3600 for finished in self.merges)
        rate = (
            Content.assemble(glyph("success"), muted(f" {merged} merged/hr"))
            if self.history
            else Content()
        )
        self.query_one(".rate", Static).update(rate)
        took = [elapsed(a) for a in self.attempts[-30:]]
        failed = sum(a["state"] == "failure" for a in self.attempts[-30:])
        stats = Content()
        if len(took) > 1:
            self.query_one(".took.spark", Sparkline).data = took
            stats = Content.assemble(
                muted(
                    f" median {human_seconds(statistics.median(took))} {SYMBOLS.dot} "
                    f"max {human_seconds(max(took))} {SYMBOLS.dot} "
                ),
                glyph("failure"),
                muted(f" {failed}/{len(took)}"),
            )
            self.query_one(".took-stats", Static).update(stats)
        self.text_cells = tuple(c.cell_length for c in (title, eta, rate, stats))
        self.fit()

    def head_width(self, depth: int | None, took: int | None, stats: bool) -> int:
        """Cells the head row needs with sparklines of these widths (None: hidden).

        Mirrors the ``.queue-head`` rules in the app CSS: widths, margins (adjacent
        margins collapse to the larger) and the ETA's ``min-width``."""
        title, eta, rate, stats_cells = self.text_cells
        boxes = [(title, 0, 0), (max(18, eta + 2), 0, 0)]  # (width, margin-left, margin-right)
        if depth is not None:
            boxes += [(5, 2, 0), (depth, 1, 1)]  # "depth" label, sparkline
        boxes.append((rate, 2, 0))
        if stats:
            boxes.append((4, 2, 0))  # "took" label
            if took is not None:
                boxes.append((took, 1, 1))
            boxes.append((stats_cells, 0, 0))
        gaps = sum(max(left[2], right[1]) for left, right in pairwise(boxes))
        return sum(box[0] for box in boxes) + boxes[0][1] + boxes[-1][2] + gaps

    def fit(self) -> None:
        """Size the sparklines to the room the head row's text leaves, so the counts
        never clip: shrink both, then drop the depth trend, then the took trend."""
        room = self.query_one(".queue-head").size.width or self.app.size.width - 4
        stats = len(self.attempts[-30:]) > 1
        want_depth = min(len(self.depth), SPARK_CELLS) if len(self.depth) > 1 else None
        want_took = min(len(self.attempts), SPARK_CELLS) if stats else None
        for depth, took in ((want_depth, want_took), (None, want_took), (None, None)):
            spare = room - self.head_width(
                None if depth is None else 0, None if took is None else 0, stats
            )
            if depth is not None and took is not None:
                depth = min(depth, max(spare // 2, spare - took))
                took = min(took, spare - depth)
            elif depth is not None:
                depth = min(depth, spare)
            elif took is not None:
                took = min(took, spare)
            # A short history keeps its full length; only a shrunk trend has a floor.
            fits = ((depth, want_depth), (took, want_took))
            if all(cells is None or cells >= min(want, SPARK_MIN) for cells, want in fits):
                break
        # Visibility is by class so that the narrow layout's CSS can still hide sparklines.
        self.set_class(depth is not None, "-depth")
        if depth is not None:
            self.query_one(".depth.spark").styles.width = depth
        self.set_class(stats, "-took")
        self.set_class(stats and took is None, "-took-flat")
        if took is not None:
            self.query_one(".took.spark").styles.width = took

    async def chain(self) -> None:
        chain = self.query_one(".chain", Horizontal)
        width = chain.size.width or self.app.size.width
        room = max(1, (width + 3) // (CHIP_MIN_WIDTH + 3))
        shown = self.entries if len(self.entries) <= room else self.entries[: max(1, room - 1)]
        signature = (
            width,
            tuple(
                (e["id"], e["state"], e.get("position"), e["title"], eta_of(e), e.get("owner"))
                for e in shown
            ),
            len(self.entries),
            tuple(e["id"] in self.by_record for e in shown),
        )
        if signature == self.built:
            return
        self.built = signature
        widgets: list[Static] = []
        for index, entry in enumerate(shown):
            if index:
                widgets.append(Static(SYMBOLS.arrow, classes="link"))
            widgets.append(
                QueueChip(self.source, entry, self.by_record.get(entry["id"]), holder=index == 0)
            )
        if len(shown) < len(self.entries):
            widgets.append(Static(f"+{len(self.entries) - len(shown)} more", classes="more"))
        await chain.remove_children()
        await chain.mount_all(widgets)
        chain.display = bool(widgets)

    async def on_resize(self) -> None:
        self.fit()
        if self.built is not None and self.built[0] != self.query_one(".chain").size.width:
            await self.chain()


# --- changes table --------------------------------------------------------- #


@dataclass
class Column:
    key: str
    label: str
    width: int  # 0 takes the remaining space
    align: str = "left"


MIN_TITLE = 10  # Narrower than this a title is not worth the columns it displaces


def change_columns(width: int, queue: bool, comfortable: bool = False) -> list[Column]:
    """Columns for a table ``width`` cells wide; narrow tables keep the essentials.

    Comfortable rows carry title, owner and PR on a second line, so those columns go.
    Where the owner column no longer fits, the title stays: it says what the change is.
    """
    roomy = width >= 96
    wide = roomy and not comfortable
    cols = [Column("glyph", "", 1), Column("ref", "CHANGE", 18 if roomy else 16)]
    if wide:
        cols += [Column("pr", "PR", 6), Column("title", "TITLE", 0), Column("owner", "OWNER", 10)]
    cols.append(Column("ci", "CI", 20 if queue else 26))
    if queue:
        cols.append(Column("queue", "QUEUE", 24 if roomy else 16))
    cols.append(Column("age", "AGE", 4, "right"))

    def spare(columns: list[Column]) -> int:
        return width - sum(c.width for c in columns) - (len(columns) - 1)

    flex = spare(cols)
    if wide and flex < MIN_TITLE:
        cols = [c for c in cols if c.key != "owner"]
        flex = spare(cols)
    if not wide or flex < MIN_TITLE:
        cols = [c for c in cols if c.key not in ("title", "owner")]
        flex = spare(cols)
        if flex >= 0:
            cols[1].width += flex
        # Too narrow: give back cells from the text columns, down to a readable minimum.
        for col in cols:
            floor = {"ref": 8, "ci": 10, "queue": 8}.get(col.key, col.width)
            take = min(-flex, col.width - floor) if flex < 0 else 0
            col.width -= take
            flex += take
    for col in cols:
        if col.width == 0:
            col.width = flex
    return cols


def change_cell(col: Column, change: Change, compact: bool) -> Content:
    match col.key:
        case "glyph":
            return Content.assemble(glyph(change.state))
        case "ref":
            name = clean(change.ref or (change.sha or change.key)[:7])
            if change.repo:  # several repositories: name the one this change is in
                return Content.assemble(dim(clean(change.repo.split("/")[-1]) + " "), (name, "b"))
            return Content.assemble((name, "b"))
        case "pr":
            return dim(f"#{change.pr}" if change.pr else "")
        case "title":
            return Content(clean(change.title))
        case "owner":
            return dim(clean(change.owner or ""))
        case "ci":
            return ci_status(change)
        case "queue":
            return Content.assemble(
                muted(f"{SYMBOLS.arrow} "), queue_status(change, "Q" if compact else "Queue")
            )
        case "age":
            return dim(short_age(ago(change.activity)))
    return Content("")


def change_detail(change: Change, width: int) -> Content:
    """Second line of a comfortable row: title, owner and PR under the change name."""
    parts = [
        part
        for part in (
            clean(change.title or ""),
            clean(change.owner or ""),
            f"#{change.pr}" if change.pr else "",
        )
        if part
    ]
    indent = 2  # under the change name, past the glyph column
    detail = dim(f" {SYMBOLS.dot} ".join(parts) or SYMBOLS.dash)
    return Content.assemble(" " * indent, fit(detail, width - indent, "left"))


class ChangeTable(Vertical):
    """Header line over an OptionList of group rows and change rows.

    Enter or a click on a group row folds or unfolds it. Done starts folded whenever
    another group has changes; a group the user has toggled keeps that choice.
    """

    def __init__(self, density: str = "compact") -> None:
        super().__init__(id="changes")
        self.changes: list[Change] = []
        self.queue = False
        self.selected: str | None = None
        self.width = 0
        self.empty = "Waiting for sources..."
        self.density = density
        self.folds: dict[str, bool] = {}  # explicit user choices by group key

    def folded(self, key: str) -> bool:
        if key in self.folds:
            return self.folds[key]
        others = any(c.group != "done" for c in self.changes)
        return key == "done" and others

    def compose(self) -> ComposeResult:
        yield Static(id="colhead")
        yield OptionList(id="rows")

    def on_resize(self) -> None:
        # Options are padded one cell either side; the list's scrollbar is one cell (app CSS).
        width = self.size.width - 3
        if width != self.width and width > 20:
            self.width = width
            self.rebuild()

    def show(self, changes: list[Change], queue: bool, selected: str | None, empty: str) -> None:
        self.changes, self.queue, self.selected, self.empty = changes, queue, selected, empty
        self.rebuild()

    def rebuild(self) -> None:
        if not self.width:
            return
        comfortable = self.density == "comfortable"
        cols = change_columns(self.width, self.queue, comfortable)
        compact = self.width < 96
        head = Content(" ").join(
            fit(Content.assemble(muted(c.label)), c.width, c.align) for c in cols
        )
        self.query_one("#colhead", Static).update(head)
        options: list[Option] = []
        for key, label, state, changes in grouped(self.changes):
            if not changes:
                continue
            folded = self.folded(key)
            marker = SYMBOLS.folded if folded else SYMBOLS.unfolded
            title = Content.assemble(
                glyph(state),
                " ",
                (label.upper(), f"b {STATE_STYLE[state]}"),
                muted(f"  {len(changes)} {marker} "),
            )
            rule = SYMBOLS.rule * max(0, self.width - title.cell_length)
            options.append(Option(Content.assemble(title, muted(rule)), id=f"g:{key}"))
            if folded:
                continue
            for change in changes:
                row = Content(" ").join(
                    fit(change_cell(c, change, compact), c.width, c.align) for c in cols
                )
                if comfortable:
                    row = Content("\n").join([row, change_detail(change, self.width)])
                options.append(Option(row, id=f"c:{change.key}"))
        if not options:
            options.append(Option(dim(self.empty), id="g:empty", disabled=True))
        rows = self.query_one("#rows", OptionList)
        rows.clear_options()
        rows.add_options(options)
        self.highlight(self.selected)

    def highlight(self, key: str | None, reveal: bool = False) -> None:
        """Move the cursor to a change; ``reveal`` unfolds its group if needed.

        A change in a folded group puts the cursor on that group's row, so a rebuild
        never leaves the list without a cursor.
        """
        if key is None:
            return
        change = next((c for c in self.changes if c.key == key), None)
        if reveal and change and self.folded(change.group):
            self.folds[change.group] = False
            self.rebuild()
        rows = self.query_one("#rows", OptionList)
        targets = [f"c:{key}"] + ([f"g:{change.group}"] if change else [])
        for target in targets:
            try:
                index = rows.get_option_index(target)
            except OptionDoesNotExist:
                continue
            if rows.highlighted != index:
                rows.highlighted = index
            return

    def toggle(self, group: str) -> None:
        self.folds[group] = not self.folded(group)
        self.rebuild()
        rows = self.query_one("#rows", OptionList)
        rows.highlighted = rows.get_option_index(f"g:{group}")

    @on(OptionList.OptionHighlighted)
    @on(OptionList.OptionSelected)
    def _highlighted(self, event) -> None:
        event.stop()
        rows = event.option_list
        # Highlight events arrive asynchronously; one queued by a rebuild can land after a
        # newer selection (a chip click) and must not drag the selection back.
        current = rows.highlighted
        if current is None or rows.get_option_at_index(current).id != event.option_id:
            return
        option_id = event.option_id or ""
        if option_id.startswith("c:"):
            self.post_message(ChangeSelected(option_id[2:]))
        elif option_id.startswith("g:") and isinstance(event, OptionList.OptionSelected):
            self.toggle(option_id[2:])


# --- inspector ------------------------------------------------------------- #


class Inspector(VerticalScroll):
    """Detail for the selection: a head, Open and Copy SHA, then titled sections."""

    def __init__(self) -> None:
        super().__init__(id="inspector")
        self.border_title = Content("inspector")
        self.pairs: list[tuple[Static, Static]] = []

    def compose(self) -> ComposeResult:
        yield Static(dim("Select a change to inspect it."), id="insp-head")
        with Horizontal(id="insp-actions"):
            yield Button("Open", id="open", compact=True, disabled=True)
            yield Button("Copy SHA", id="copy", compact=True, disabled=True)
        yield Vertical(id="sections")

    async def show(
        self,
        head: Content,
        sections: list[tuple[Content, Content]],
        *,
        url: bool,
        sha: bool,
        reset: bool,
    ) -> None:
        self.query_one("#insp-head", Static).update(head)
        self.query_one("#open", Button).disabled = not url
        self.query_one("#copy", Button).disabled = not sha
        holder = self.query_one("#sections", Vertical)
        while len(self.pairs) < len(sections):
            pair = (Static(classes="section-title"), Static(classes="section-body"))
            self.pairs.append(pair)
            await holder.mount_all(pair)
        for index, (title, body) in enumerate(self.pairs):
            visible = index < len(sections)
            title.display = body.display = visible
            if visible:
                title.update(sections[index][0])
                body.update(sections[index][1])
        if reset:
            self.scroll_home(animate=False)

    def section(self, index: int, body: Content) -> None:
        """Replace one section body (lazy details) without touching the others."""
        if index < len(self.pairs):
            self.pairs[index][1].update(body)
