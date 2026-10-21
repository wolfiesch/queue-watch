"""Textual dashboard over any list of sources; views come from snapshot v1."""

from __future__ import annotations

import json
import time
import webbrowser
from datetime import datetime
from typing import Any, ClassVar
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import (
    Button,
    DataTable,
    Footer,
    Header,
    Input,
    Select,
    Static,
    TabbedContent,
    TabPane,
)

from .adapters import Adapter
from .config import Source
from .model import ACTIVE, ATTENTION, lookup, views_for
from .process import clean

PACIFIC = ZoneInfo("America/Los_Angeles")
COLORS = {
    "success": "#7bdcb5",
    "running": "#70c8ff",
    "queued": "#f6cc78",
    "blocked": "#f6cc78",
    "failure": "#ff8197",
    "cancelled": "#a5aec3",
    "neutral": "#a5aec3",
    "unknown": "#a5aec3",
}
FETCH_ERRORS = (OSError, RuntimeError, ValueError, KeyError, TypeError)


def clock(value: str | None) -> str:
    try:
        return datetime.fromisoformat(value).astimezone(PACIFIC).strftime("%-I:%M:%S %p %Z")
    except (ValueError, TypeError):
        return "unknown"


def age(value: str | None) -> str:
    try:
        seconds = time.time() - datetime.fromisoformat(value).timestamp()
    except (ValueError, TypeError):
        return "?"
    return duration(seconds)


def duration(value: object) -> str:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return "-"
    seconds = max(0, int(value))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m {seconds % 60:02}s"
    return f"{seconds // 3600}h {seconds % 3600 // 60:02}m"


def badge(record: dict) -> Text:
    label = record.get("raw_state") or record["state"]
    return Text(clean(label).replace("_", " "), style=COLORS[record["state"]])


def cell(record: dict, column: dict) -> Text | str:
    value = lookup(record, column["key"])
    match column.get("format", "text"):
        case "state":
            return badge(record)
        case "clock":
            return clock(value)
        case "age":
            return age(value)
        case "seconds":
            return f"{value:.1f}s" if isinstance(value, (int, float)) else "-"
        case "duration":
            return duration(value)
        case "sha":
            return Text(clean(value or "")[:8])
    return Text(clean(value) if value is not None else "-")


class QueueWatch(App):
    TITLE = "QUEUE WATCH"
    SUB_TITLE = "read-only"
    CSS = """
    Screen { background: #10141e; color: #dce4f3; }
    Header { background: #192235; color: #8bd5ff; }
    #metrics { height: 5; margin: 1 2 0 2; }
    .metric { width: 1fr; border: round #33435d; padding: 0 2; margin-right: 1; }
    .metric:last-child { margin-right: 0; }
    #toolbar { height: 3; margin: 1 2; }
    #search { width: 1fr; margin-right: 1; }
    #filter { width: 22; margin-right: 1; }
    Button { min-width: 10; margin-right: 1; }
    #body { margin: 0 2; height: 1fr; }
    #tables { width: 3fr; min-width: 34; }
    TabbedContent { height: 1fr; }
    ContentSwitcher { height: 1fr; }
    TabPane { padding: 0; height: 1fr; }
    DataTable { height: 1fr; background: #141b28; }
    DataTable > .datatable--header { background: #25334b; color: #a9c7ec; }
    DataTable > .datatable--cursor { background: #304d71; color: #ffffff; }
    DataTable > .datatable--hover { background: #22354e; }
    #inspector { width: 2fr; min-width: 28; border: round #33435d; margin-left: 1; }
    #detail-scroll { height: 1fr; padding: 0 1; }
    #detail { height: auto; }
    #open { margin: 0 1 1 1; width: 1fr; }
    #health { height: auto; max-height: 5; margin: 1 2 0 2; color: #a5aec3; }
    #hint { height: 1; margin: 0 2; color: #8190a8; }
    Footer { background: #192235; }
    .narrow #body { layout: vertical; }
    .narrow #tables { width: 1fr; height: 2fr; }
    .narrow #inspector { width: 1fr; height: 1fr; margin: 1 0 0 0; }
    .narrow #metrics { margin: 0 1; }
    .narrow .metric { padding: 0 1; }
    .narrow #toolbar { margin: 0 1; }
    .narrow #filter { width: 17; }
    .narrow Button { min-width: 8; }
    .short #metrics, .short #hint { display: none; }
    .short #toolbar { margin: 0 1; }
    .short #health { max-height: 3; margin: 0 1; }
    .short #tables, .short #inspector { height: 1fr; }
    .short #open { height: 1; min-height: 1; margin: 0; border: none; padding: 0; }
    """
    BINDINGS: ClassVar = [
        Binding("q", "quit", "Quit"),
        Binding("r", "refresh", "Refresh"),
        Binding("p", "pause", "Pause"),
        Binding("/", "search", "Search"),
        Binding("o", "open", "Open"),
        Binding("escape", "clear", "Clear search"),
    ]

    def __init__(self, sources: list[Source]):
        super().__init__()
        self.sources = {source.id: source for source in sources}
        self.snapshots: dict[str, dict] = {}
        self.errors: dict[str, str] = {}
        self.updated: dict[str, float] = {}
        self.busy: set[str] = set()
        self.paused = False
        # Pane name -> (source id, view); names are generated, never user text.
        self.panes: dict[str, tuple[str, dict]] = {}
        self.answered: set[str] = set()
        self.records: dict[str, dict[str, dict]] = {}
        self.selection: tuple[str, str] | None = None
        self.child_cache: dict[tuple[str, str], tuple[float, list]] = {}
        self.detail_generation = 0
        self.rendering = False

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal(id="metrics"):
            for name in ("running", "queued", "attention", "sources"):
                yield Static(f"{name.upper()}\n...", id=name, classes="metric")
        with Horizontal(id="toolbar"):
            yield Input(placeholder="Search title, branch, owner...", id="search")
            yield Select(
                [
                    ("All states", "all"),
                    ("Active only", "active"),
                    ("Needs attention", "attention"),
                ],
                value="all",
                allow_blank=False,
                id="filter",
            )
            yield Button("Refresh", id="refresh", variant="primary")
            yield Button("Pause", id="pause")
        with Horizontal(id="body"):
            with Vertical(id="tables"):
                yield TabbedContent(id="tabs")
            with Vertical(id="inspector"):
                with VerticalScroll(id="detail-scroll"):
                    yield Static(
                        "Select a row to inspect its source record.",
                        id="detail",
                        markup=False,
                    )
                yield Button("Open in browser", id="open", disabled=True)
        yield Static("Connecting to sources...", id="health", markup=False)
        yield Static(
            "Click a row to inspect  |  Wheel to scroll  |  Read-only: nothing is changed",
            id="hint",
        )
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#inspector").border_title = "INSPECTOR"
        for source in self.sources.values():
            self.set_interval(source.interval, lambda s=source.id: self.poll(s))
        self.set_interval(1, self.render_health)
        self.action_refresh()

    def on_resize(self, event) -> None:
        self.screen.set_class(event.size.width < 110, "narrow")
        self.screen.set_class(event.size.height < 30, "short")

    def poll(self, source_id: str) -> None:
        if not self.paused:
            self.fetch(source_id)

    @work
    async def fetch(self, source_id: str) -> None:
        if source_id in self.busy:
            return
        self.busy.add(source_id)
        self.render_health()
        try:
            snapshot = await self.sources[source_id].snapshot()
            self.snapshots[source_id] = snapshot
            self.updated[source_id] = time.monotonic()
            self.errors.pop(source_id, None)
            await self.ensure_panes(source_id)
            self.rebuild()
        except FETCH_ERRORS as error:
            self.errors[source_id] = clean(str(error) or type(error).__name__)
        finally:
            self.busy.discard(source_id)
            self.render_health()

    async def ensure_panes(self, source_id: str) -> None:
        """Add one tab per (source, view) the first time that view appears."""
        tabs = self.query_one("#tabs", TabbedContent)
        known = {(sid, view["kind"]) for sid, view in self.panes.values()}
        many = len(self.sources) > 1
        order = list(self.sources)
        for view in views_for(self.snapshots[source_id]):
            if (source_id, view["kind"]) in known:
                continue
            # Sources answer in any order; keep tabs in configuration order.
            later = [
                f"{pane}-tab"
                for pane, (sid, _) in self.panes.items()
                if order.index(sid) > order.index(source_id)
            ]
            name = f"pane{len(self.panes)}"
            self.panes[name] = (source_id, view)
            self.records[name] = {}
            table = DataTable(id=name, cursor_type="row", zebra_stripes=True)
            for column in view["columns"]:
                table.add_column(column.get("label", column["key"]), width=column.get("width"))
            label = view.get("label") or view["kind"]
            if many:
                label = f"{self.sources[source_id].label} · {label}"
            pane = TabPane(clean(label), table, id=f"{name}-tab")
            await tabs.add_pane(pane, before=later[0] if later else None)
        if source_id not in self.answered and self.panes:
            # During each source's first answer, show the first tab in order.
            self.answered.add(source_id)
            first = min(self.panes, key=lambda pane: order.index(self.panes[pane][0]))
            tabs.active = f"{first}-tab"
            self.query_one(f"#{first}", DataTable).focus()

    def render_health(self) -> None:
        if not self.is_running:
            return
        lines = []
        for source_id, source in self.sources.items():
            snapshot = self.snapshots.get(source_id, {})
            elapsed = time.monotonic() - self.updated.get(source_id, time.monotonic())
            state = "PAUSED" if self.paused else "LIVE"
            if source_id not in self.updated:
                state = "NO DATA"
            elif elapsed > max(3 * source.interval, 20):
                state = "STALE"
            if source_id in self.errors:
                state = "STALE" if source_id in self.updated else "UNAVAILABLE"
            line = f"{source.label.upper()} {state} | last success {clock(snapshot.get('observed_at'))}"
            if source_id in self.updated:
                line += f" ({int(elapsed)}s ago)"
            if source_id in self.busy:
                line += " | refreshing"
            if source_id in self.errors:
                line += " | " + self.errors[source_id]
            if snapshot.get("warnings"):
                line += " | " + clean("; ".join(snapshot["warnings"]))
            lines.append(line)
        self.query_one("#health", Static).update("\n".join(lines))

    def matches(self, record: dict) -> bool:
        query = self.query_one("#search", Input).value.casefold()
        if query and query not in json.dumps(record, ensure_ascii=False).casefold():
            return False
        mode = self.query_one("#filter", Select).value
        if mode == "active":
            return record["state"] in ACTIVE
        if mode == "attention":
            return record["state"] in ATTENTION
        return True

    def rebuild(self) -> None:
        records = [r for s in self.snapshots.values() for r in s.get("records", [])]
        live = sum(
            source_id in self.updated and source_id not in self.errors for source_id in self.sources
        )
        metrics = {
            "running": sum(r["state"] == "running" for r in records),
            "queued": sum(r["state"] == "queued" for r in records),
            "attention": sum(r["state"] in ATTENTION for r in records),
            "sources": f"{live}/{len(self.sources)} live",
        }
        for name, value in metrics.items():
            shown = value if self.snapshots else "..."
            self.query_one(f"#{name}", Static).update(Text(f"{name.upper()}\n{shown}"))
        self.rendering = True
        for name, (source_id, view) in self.panes.items():
            kind = view["kind"]
            rows = [
                r
                for r in self.snapshots.get(source_id, {}).get("records", [])
                if r["kind"] == kind and self.matches(r)
            ]
            self.records[name] = {r["id"]: r for r in rows}
            table = self.query_one(f"#{name}", DataTable)
            old_key = None
            if table.row_count:
                old_key = table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value
            scroll_x, scroll_y = table.scroll_x, table.scroll_y
            table.clear()
            for record in rows:
                table.add_row(*(cell(record, c) for c in view["columns"]), key=record["id"])
            keys = [r["id"] for r in rows]
            if keys:
                table.move_cursor(row=keys.index(old_key) if old_key in keys else 0, animate=False)
            table.scroll_to(x=scroll_x, y=scroll_y, animate=False)
        self.rendering = False
        self.show_current()

    def active_pane(self) -> str | None:
        active = self.query_one("#tabs", TabbedContent).active
        name = active.removesuffix("-tab") if active else ""
        return name if name in self.panes else None

    def show_current(self) -> None:
        name = self.active_pane()
        if name is None:
            return
        table = self.query_one(f"#{name}", DataTable)
        if table.row_count:
            key = table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value
            self.show_detail(name, key)
            return
        self.selection = None
        self.detail_generation += 1
        self.query_one("#open", Button).disabled = True
        self.query_one("#detail", Static).update("No rows match this view.")

    @on(DataTable.RowHighlighted)
    @on(DataTable.RowSelected)
    def select_row(self, event) -> None:
        if self.is_running and not self.rendering:
            name = event.data_table.id
            if name == self.active_pane() and event.row_key.value in self.records[name]:
                self.show_detail(name, event.row_key.value)

    @on(TabbedContent.TabActivated)
    def tab_changed(self) -> None:
        if self.is_running and self.is_mounted:
            self.show_current()

    @on(Input.Changed, "#search")
    @on(Select.Changed, "#filter")
    def filters_changed(self) -> None:
        if self.is_running and self.is_mounted:
            self.rebuild()

    def show_detail(self, name: str, key: str) -> None:
        record = self.records[name].get(key)
        if record is None:
            return
        source_id = self.panes[name][0]
        changed = self.selection != (name, key)
        self.selection = (name, key)
        self.detail_generation += 1
        self.query_one("#open", Button).disabled = not safe_url(record.get("url"))
        body = "\n".join(describe(record))
        note = self.snapshots.get(source_id, {}).get("note")
        detail = self.query_one("#detail", Static)
        adapter = self.sources[source_id].adapter
        if record.get("children") or type(adapter).children is not Adapter.children:
            detail.update(body + "\n\nDETAILS\nLoading...")
            self.fetch_children(source_id, record, self.detail_generation, body, note)
        else:
            detail.update(body + (f"\n\n{clean(note)}" if note else ""))
        if changed:
            self.query_one("#detail-scroll", VerticalScroll).scroll_home(animate=False)

    @work(group="children", exclusive=True)
    async def fetch_children(
        self, source_id: str, record: dict, generation: int, body: str, note: str | None
    ) -> None:
        cache_key = (source_id, record["id"])
        try:
            cached = self.child_cache.get(cache_key)
            if cached and time.monotonic() - cached[0] < 15:
                children = cached[1]
            else:
                children = await self.sources[source_id].adapter.children(record)
                self.child_cache[cache_key] = (time.monotonic(), children)
                if len(self.child_cache) > 100:
                    self.child_cache.pop(next(iter(self.child_cache)))
            lines = []
            for child in children:
                lines.append(
                    f"{clean(child.get('raw_state') or child['state'])}  {clean(child['title'])}"
                )
                for field, value in (child.get("fields") or {}).items():
                    lines.append(
                        f"  {field.replace('_', ' ')}: {clean(value if value is not None else 'not assigned')}"
                    )
                for step in child.get("children") or []:
                    if step["state"] in {"running", "failure"}:
                        lines.append(
                            f"  > {clean(step['title'])}: {clean(step.get('raw_state') or step['state'])}"
                        )
            text = body + "\n\nDETAILS\n" + ("\n".join(lines) or "None reported yet.")
        except FETCH_ERRORS as error:
            text = body + "\n\nDetails unavailable: " + clean(str(error) or type(error).__name__)
        if note:
            text += f"\n\n{clean(note)}"
        if generation == self.detail_generation:
            self.query_one("#detail", Static).update(text)

    @on(Button.Pressed)
    def button_pressed(self, event: Button.Pressed) -> None:
        {
            "refresh": self.action_refresh,
            "pause": self.action_pause,
            "open": self.action_open,
        }[event.button.id]()

    def action_refresh(self) -> None:
        for source_id in self.sources:
            self.fetch(source_id)

    def action_pause(self) -> None:
        self.paused = not self.paused
        self.query_one("#pause", Button).label = "Resume" if self.paused else "Pause"
        if not self.paused:
            self.action_refresh()
        self.render_health()

    def action_search(self) -> None:
        self.query_one("#search", Input).focus()

    def action_clear(self) -> None:
        self.query_one("#search", Input).value = ""
        self.query_one("#filter", Select).value = "all"
        self.query_one("#search", Input).blur()

    def action_open(self) -> None:
        if self.selection:
            name, key = self.selection
            url = self.records[name].get(key, {}).get("url")
            if safe_url(url):
                webbrowser.open(url)


def safe_url(url: Any) -> bool:
    if not isinstance(url, str):
        return False
    parsed = urlparse(url)
    return parsed.scheme == "https" and bool(parsed.netloc)


def describe(record: dict) -> list[str]:
    lines = [clean(record["title"])]
    if record.get("subtitle") and record["subtitle"] != record["title"]:
        lines.append(clean(record["subtitle"]))
    lines.append("")
    lines.append(f"State: {clean(record.get('raw_state') or record['state'])} ({record['state']})")
    for key, label in (
        ("ref", "Branch"),
        ("sha", "SHA"),
        ("owner", "Owner"),
        ("position", "Position"),
    ):
        if record.get(key) is not None:
            lines.append(f"{label}: {clean(record[key])}")
    for key in ("created_at", "updated_at", "started_at", "finished_at"):
        if record.get(key):
            lines.append(f"{key.removesuffix('_at').capitalize()}: {clock(record[key])}")
    for field, value in (record.get("fields") or {}).items():
        if isinstance(value, (dict, list)):
            value = json.dumps(value)
        lines.append(f"{field.replace('_', ' ')}: {clean(value)}")
    return lines
