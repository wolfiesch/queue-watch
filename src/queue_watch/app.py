"""Textual dashboard: changes across sources, merge queues, and per-source tables.

Layout (design/README.md): a status line with one pill per source, a strip of
bordered chips per merge queue, then a Changes tab (records correlated by ref or
sha, grouped by what needs attention) beside an inspector. Every view a source
declares stays reachable as its own tab.
"""

from __future__ import annotations

import itertools
import json
import time
import webbrowser
from typing import Any, ClassVar
from urllib.parse import urlparse

from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.content import Content
from textual.events import Resize
from textual.widgets import Button, DataTable, Footer, Input, Static, TabbedContent, TabPane

from . import widgets
from .adapters import Adapter
from .changes import Change, Hit, correlate, grouped
from .config import Source
from .model import lookup, views_for
from .process import clean
from .widgets import (
    MUTED,
    STATE_STYLE,
    ChangeSelected,
    ChangeTable,
    ChipClicked,
    Health,
    Inspector,
    QueueStrip,
    StatusBar,
    ago,
    ci_status,
    clock,
    dim,
    duration,
    elapsed,
    eta_of,
    eta_text,
    fit,
    glyph,
    human_seconds,
    lines,
    muted,
    queue_status,
    raw,
    rich_style,
    short_age,
    state_text,
)

FETCH_ERRORS = (OSError, RuntimeError, ValueError, KeyError, TypeError)
CHANGES = "changes-tab"
CHILD_TTL = 15
BAR = 8  # job duration bar cells; a job line fits the narrowest inspector

CSS = """
Screen { background: $surface; }
#status { height: 1; padding: 0 1; background: $panel; color: $foreground; }
#status #brand { width: auto; margin-right: 2; color: $primary; }
#status .pill { width: auto; padding: 0 1; margin-right: 1; }
#status .pill.live { background: $success 25%; color: $text-success; }
#status .pill.paused { background: $primary 20%; color: $text-primary; }
#status .pill.waiting { background: $panel; color: $text-muted; }
#status .pill.stale { background: $warning 30%; color: $text-warning; }
#status .pill.unavailable { background: $error 30%; color: $text-error; }
#status .counts { width: 1fr; text-align: right; }
#status #counts-short { display: none; }
.-narrow #status #counts-long { display: none; }
.-narrow #status #counts-short { display: block; }

#queues { height: auto; }
.queue { height: auto; padding: 0 1; border-bottom: solid $foreground 20%; }
.queue-head { height: 1; }
.queue-head .queue-title { width: auto; }
.queue-head .queue-eta { width: 1fr; min-width: 18; padding-left: 2; }
.queue-head .trend-label { width: auto; margin-left: 2; }
.-narrow .queue-head .trend-label { display: none; }
.queue-head .trend { margin: 0 0 0 1; }
.queue-head .trend-stats { width: auto; }
Sparkline > .sparkline--max-color { color: $error; }
Sparkline > .sparkline--min-color { color: $success; }
.chain { height: auto; }
.chain .link { width: 3; height: 4; content-align: center middle; color: $text-muted; }
.chain .more { width: auto; height: 4; content-align: center middle; color: $text-muted; padding: 0 1; }
.chip {
    width: 1fr; height: 4; padding: 0 1; background: $surface;
    border: round $foreground 30%; border-title-color: $text-muted;
    text-wrap: nowrap; text-overflow: ellipsis;
}
.chip:hover { border: round $primary 60%; }
.chip.holder { border: heavy $primary; background: $primary 14%; border-title-color: $primary; }
.-ascii .chip { border: ascii $foreground 40%; }
.-ascii .chip.holder { border: ascii $primary; }

#search { display: none; height: 1; border: none; padding: 0 1; margin: 0 1; background: $panel; }
#body { height: 1fr; }
TabbedContent { width: 1fr; height: 1fr; }
TabbedContent > ContentSwitcher { height: 1fr; }
TabPane { padding: 0; height: 1fr; }
Tabs { height: 2; }
#changes { height: 1fr; }
#colhead { height: 1; padding: 0 1; background: $panel; }
OptionList {
    height: 1fr; background: $surface; border: none; padding: 0; scrollbar-size-vertical: 1;
}
OptionList:focus { border: none; }
OptionList > .option-list--option { padding: 0 1; }
OptionList > .option-list--option-highlighted { background: $primary 28%; color: $foreground; text-style: none; }
OptionList:focus > .option-list--option-highlighted { background: $primary 40%; color: $foreground; text-style: none; }
OptionList > .option-list--option-hover { background: $primary 12%; }
OptionList > .option-list--option-disabled { color: $text-muted; }
DataTable { height: 1fr; background: $surface; }
DataTable > .datatable--header { background: $panel; color: $text-muted; text-style: bold; }
DataTable > .datatable--cursor { background: $primary 30%; color: $foreground; }

#inspector {
    width: 32%; min-width: 40; max-width: 64; height: 1fr; padding: 0 1;
    border-left: solid $foreground 20%; background: $surface; scrollbar-size-vertical: 1;
}
#insp-head { height: auto; padding-bottom: 1; }
#insp-actions { height: auto; padding-bottom: 1; }
#insp-actions Button { margin-right: 1; background: $primary 30%; color: $foreground; }
#sections { height: auto; }
.section-title { background: $panel; padding: 0 1; margin-top: 1; height: 1; }
.section-body { height: auto; padding: 0 1; }

#notices { display: none; height: auto; max-height: 4; padding: 0 1; color: $text-warning; background: $panel; }
"""


def safe_url(url: Any) -> bool:
    if not isinstance(url, str):
        return False
    parsed = urlparse(url)
    return parsed.scheme == "https" and bool(parsed.netloc)


def cell(app: App, record: dict, column: dict) -> Text:
    value = lookup(record, column["key"])
    match column.get("format", "text"):
        case "state":
            return state_text(app, record)
        case "clock":
            return Text(clock(value))
        case "age":
            return Text(short_age(ago(value)), style=rich_style(app, MUTED))
        case "seconds":
            return Text(f"{value:.1f}s" if isinstance(value, (int, float)) else "-")
        case "duration":
            return Text(duration(value))
        case "sha":
            return Text(clean(value or "")[:8], style=rich_style(app, MUTED))
    return Text(clean(value) if value is not None else "-")


def describe(record: dict) -> list[Content]:
    rows = [Content.assemble(muted("State  "), glyph(record["state"]), f" {raw(record)}")]
    for key, label in (("ref", "Branch"), ("sha", "SHA"), ("owner", "Owner"), ("position", "#")):
        if record.get(key) is not None:
            rows.append(Content.assemble(muted(f"{label}  "), clean(record[key])))
    for key in ("created_at", "updated_at", "started_at", "finished_at"):
        if record.get(key):
            label = key.removesuffix("_at").capitalize()
            rows.append(Content.assemble(muted(f"{label}  "), clock(record[key])))
    for name, value in (record.get("fields") or {}).items():
        if isinstance(value, (dict, list)):
            value = json.dumps(value)
        shown = clean(value) if value is not None else "-"
        rows.append(Content.assemble(muted(f"{clean(name).replace('_', ' ')}  "), shown))
    return rows


def job_tree(jobs: list[dict]) -> list[Content]:
    """Jobs with duration bars; steps only for jobs that are running or failed."""
    took = [elapsed(job) for job in jobs]
    scale = max((t for t in took if t), default=1)
    rows: list[Content] = []
    for index, (job, seconds) in enumerate(zip(jobs, took, strict=True)):
        last = index == len(jobs) - 1
        style = STATE_STYLE.get(job["state"], MUTED)
        if seconds:
            filled = max(1, round(BAR * seconds / scale))
            bar = Content.assemble(
                (widgets.SYMBOLS.bar_full * filled, style),
                muted(widgets.SYMBOLS.bar_empty * (BAR - filled)),
            )
            length = human_seconds(seconds)
        else:
            bar, length = dim(widgets.SYMBOLS.bar_empty * BAR), widgets.SYMBOLS.dash
        rows.append(
            Content.assemble(
                muted(f"{widgets.SYMBOLS.elbow if last else widgets.SYMBOLS.tee} "),
                glyph(job["state"]),
                " ",
                fit(Content(clean(job["title"])), 14),
                " ",
                bar,
                " ",
                fit(Content(length), 6, "right"),
            )
        )
        if job["state"] in ("running", "failure"):
            trunk = "  " if last else f"{widgets.SYMBOLS.pipe} "
            for step in job.get("children") or []:
                if step["state"] in ("running", "failure"):
                    rows.append(
                        Content.assemble(
                            muted(f"{trunk}  "), glyph(step["state"]), f" {clean(step['title'])}"
                        )
                    )
    return rows


class QueueWatch(App):
    TITLE = "queue-watch"
    CSS = CSS
    BINDINGS: ClassVar = [
        Binding("q", "quit", "Quit"),
        Binding("r", "refresh", "Refresh"),
        Binding("p", "pause", "Pause"),
        Binding("slash", "search", "Search"),
        Binding("escape", "clear", "Clear", show=False),
        Binding("o", "open", "Open"),
        Binding("c", "copy_sha", "Copy SHA"),
        Binding("1", "tab(0)", "Changes"),
        *(Binding(str(n), f"tab({n - 1})", show=False) for n in range(2, 10)),
    ]

    def __init__(self, sources: list[Source], *, theme: str = "tokyo-night", ascii: bool = False):
        super().__init__()
        self.sources = {source.id: source for source in sources}
        self.order = list(self.sources)
        self.initial_theme = theme
        if ascii:
            widgets.use_ascii()
        self.set_class(ascii, "-ascii")
        self.snapshots: dict[str, dict] = {}
        self.errors: dict[str, str] = {}
        self.updated: dict[str, float] = {}
        self.busy: set[str] = set()
        self.paused = False
        # Pane name -> (source id, view); names are generated, never user text.
        self.panes: dict[str, tuple[str, dict]] = {}
        self.pane_ids = itertools.count()
        self.records: dict[str, dict[str, dict]] = {}
        self.strips: dict[str, QueueStrip] = {}
        self.changes: dict[str, Change] = {}
        self.by_record: dict[tuple[str, str], Change] = {}
        self.selected: str | None = None
        self.shown: tuple | None = None
        self.child_cache: dict[tuple[str, str], tuple[float, list]] = {}
        self.generation = 0
        self.rendering = False
        self.settled = False

    def compose(self) -> ComposeResult:
        yield StatusBar(len(self.sources))
        yield Vertical(id="queues")
        yield Input(placeholder="Search changes and records (esc to clear)", id="search")
        with Horizontal(id="body"):
            with TabbedContent(id="tabs", initial=CHANGES), TabPane("Changes", id=CHANGES):
                yield ChangeTable()
            yield Inspector()
        yield Static(id="notices")
        yield Footer()

    def on_mount(self) -> None:
        if self.initial_theme in self.available_themes:
            self.theme = self.initial_theme
        self.theme_changed_signal.subscribe(self, lambda _theme: self.rebuild_tables())
        for source in self.sources.values():
            self.set_interval(source.interval, lambda s=source.id: self.poll(s))
        self.set_interval(1, self.render_status)
        self.render_status()
        self.query_one("#rows").focus()
        self.action_refresh()

    def on_resize(self, event: Resize) -> None:
        self.set_class(event.size.width < 120, "-narrow")

    # --- polling ----------------------------------------------------------- #

    def poll(self, source_id: str) -> None:
        if not self.paused:
            self.fetch(source_id)

    @work
    async def fetch(self, source_id: str) -> None:
        if source_id in self.busy:
            return
        self.busy.add(source_id)
        try:
            snapshot = await self.sources[source_id].snapshot()
            self.snapshots[source_id] = snapshot
            self.updated[source_id] = time.monotonic()
            self.errors.pop(source_id, None)
            await self.ensure_panes(source_id)
            await self.ensure_strip(source_id)
        except FETCH_ERRORS as error:
            self.errors[source_id] = clean(str(error) or type(error).__name__)
        finally:
            self.busy.discard(source_id)
        await self.rebuild()
        self.settle()
        self.render_status()

    def settle(self) -> None:
        """Once every source has answered, leave an empty Changes tab for real data."""
        if self.settled or any(s not in self.updated and s not in self.errors for s in self.order):
            return
        self.settled = True
        tabs = self.query_one("#tabs", TabbedContent)
        if not self.changes and self.panes and tabs.active == CHANGES:
            first = min(self.panes, key=lambda pane: self.order.index(self.panes[pane][0]))
            tabs.active = f"{first}-tab"
            self.query_one(f"#{first}", DataTable).focus()

    async def ensure_panes(self, source_id: str) -> None:
        """Add one tab per (source, view) the first time that view appears."""
        tabs = self.query_one("#tabs", TabbedContent)
        known = {(sid, view["kind"]) for sid, view in self.panes.values()}
        for view in views_for(self.snapshots[source_id]):
            if (source_id, view["kind"]) in known:
                continue
            # Sources answer in any order; keep tabs in configuration order.
            later = [
                f"{pane}-tab"
                for pane, (sid, _) in self.panes.items()
                if self.order.index(sid) > self.order.index(source_id)
            ]
            name = f"pane{next(self.pane_ids)}"
            table = DataTable(id=name, cursor_type="row")
            for column in view["columns"]:
                table.add_column(column.get("label", column["key"]), width=column.get("width"))
            pane = TabPane(self.pane_label(source_id, view, 0), table, id=f"{name}-tab")
            await tabs.add_pane(pane, before=later[0] if later else None)
            # Register only once mounted: other fetches rebuild tables concurrently.
            self.records[name] = {}
            self.panes[name] = (source_id, view)

    async def ensure_strip(self, source_id: str) -> None:
        if source_id in self.strips:
            return
        snapshot = self.snapshots[source_id]
        kinds = {r["kind"] for r in snapshot["records"]} | {v["kind"] for v in views_for(snapshot)}
        if "queue_entry" not in kinds:
            return
        strip = QueueStrip(source_id, self.sources[source_id].label)
        later = [
            self.strips[sid]
            for sid in self.order[self.order.index(source_id) + 1 :]
            if sid in self.strips
        ]
        await self.query_one("#queues", Vertical).mount(strip, before=later[0] if later else None)
        self.strips[source_id] = strip

    def pane_label(self, source_id: str, view: dict, count: int) -> str:
        label = view.get("label") or view["kind"]
        if len(self.sources) > 1:
            label = f"{self.sources[source_id].label} {widgets.SYMBOLS.dot} {label}"
        return f"{clean(label)} {count}"

    # --- rendering ------------------------------------------------------- #

    def query_text(self) -> str:
        return self.query_one("#search", Input).value.casefold()

    def matches(self, record: dict) -> bool:
        query = self.query_text()
        return not query or query in json.dumps(record, ensure_ascii=False).casefold()

    def change_matches(self, change: Change) -> bool:
        query = self.query_text()
        if not query:
            return True
        hits = (*change.runs, *change.entries, *change.attempts)
        return query in change.key.casefold() or any(self.matches(hit.record) for hit in hits)

    async def rebuild(self) -> None:
        snapshots = {s: self.snapshots[s] for s in self.order if s in self.snapshots}
        changes = correlate(snapshots)
        self.changes = {change.key: change for change in changes}
        self.by_record = {
            (hit.source, hit.record["id"]): change
            for change in changes
            for hit in (*change.runs, *change.entries, *change.attempts)
        }
        visible = [change for change in changes if self.change_matches(change)]
        ordered = [change for *_, members in grouped(visible) for change in members]
        if self.selected not in {change.key for change in ordered}:
            self.selected = ordered[0].key if ordered else None
        empty = "No changes match the search." if self.query_text() else "No changes yet."
        if not snapshots:
            empty = "Waiting for sources..."
        queue = any(change.entries or change.attempts for change in changes)
        self.query_one(ChangeTable).show(visible, queue, self.selected, empty)
        tabs = self.query_one("#tabs", TabbedContent)
        tabs.get_tab(CHANGES).label = f"Changes {len(visible)}"
        for source_id, strip in self.strips.items():
            records = snapshots.get(source_id, {}).get("records", [])
            mine = {
                rid: change for (sid, rid), change in self.by_record.items() if sid == source_id
            }
            await strip.show(records, mine)
        self.rebuild_tables()
        await self.show_selection()

    def rebuild_tables(self) -> None:
        self.rendering = True
        tabs = self.query_one("#tabs", TabbedContent)
        for name, (source_id, view) in self.panes.items():
            rows = [
                r
                for r in self.snapshots.get(source_id, {}).get("records", [])
                if r["kind"] == view["kind"] and self.matches(r)
            ]
            self.records[name] = {r["id"]: r for r in rows}
            tabs.get_tab(f"{name}-tab").label = self.pane_label(source_id, view, len(rows))
            table = self.query_one(f"#{name}", DataTable)
            old_key = None
            if table.row_count:
                old_key = table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value
            scroll_x, scroll_y = table.scroll_x, table.scroll_y
            table.clear()
            for record in rows:
                table.add_row(*(cell(self, record, c) for c in view["columns"]), key=record["id"])
            keys = [r["id"] for r in rows]
            if keys:
                table.move_cursor(row=keys.index(old_key) if old_key in keys else 0, animate=False)
            table.scroll_to(x=scroll_x, y=scroll_y, animate=False)
        self.rendering = False

    def health(self) -> list[Health]:
        items = []
        now = time.monotonic()
        for source_id, source in self.sources.items():
            age = now - self.updated[source_id] if source_id in self.updated else None
            if age is None:
                word = "unavailable" if source_id in self.errors else "waiting"
            elif source_id in self.errors or age > max(3 * source.interval, 20):
                word = "stale"
            else:
                word = "paused" if self.paused else "live"
            items.append(Health(source.label, word, age))
        return items

    def render_status(self) -> None:
        if not self.is_running:
            return
        counts = None
        if self.snapshots:
            counts = {
                key: len(members) for key, _, _, members in grouped(list(self.changes.values()))
            }
        self.query_one(StatusBar).show(self.health(), counts)
        notes = []
        for source_id, source in self.sources.items():
            problems = []
            if source_id in self.errors:
                problems.append(self.errors[source_id])
            problems += self.snapshots.get(source_id, {}).get("warnings", [])
            if problems:
                notes.append(f"{clean(source.label)}: {clean('; '.join(problems))}")
        notices = self.query_one("#notices", Static)
        notices.update(Content("\n".join(notes)))
        notices.display = bool(notes)

    # --- selection and inspector ------------------------------------------- #

    def active_pane(self) -> str | None:
        """Pane name of the active source tab, or None on the Changes tab."""
        active = self.query_one("#tabs", TabbedContent).active
        name = (active or "").removesuffix("-tab")
        return name if name in self.panes else None

    def current_record(self, name: str) -> dict | None:
        table = self.query_one(f"#{name}", DataTable)
        if not table.row_count:
            return None
        key = table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value
        return self.records[name].get(key)

    def selection(self) -> tuple[str | None, str | None]:
        """(url, sha) of whatever the inspector shows."""
        name = self.active_pane()
        if name is None:
            change = self.changes.get(self.selected or "")
            return (change.url, change.sha) if change else (None, None)
        record = self.current_record(name) or {}
        return record.get("url"), record.get("sha")

    async def show_selection(self, reset: bool = False) -> None:
        if not self.is_running:
            return
        inspector = self.query_one(Inspector)
        name = self.active_pane()
        if name is None:
            change = self.changes.get(self.selected or "")
            target = ("change", self.selected)
            if change is None:
                await inspector.show(
                    dim("No change selected."), [], url=False, sha=False, reset=True
                )
                self.shown = target
                return
            head, sections, lazy = self.change_view(change)
        else:
            source_id = self.panes[name][0]
            record = self.current_record(name)
            target = ("record", name, record["id"] if record else None)
            if record is None:
                await inspector.show(
                    dim("No rows match this view."), [], url=False, sha=False, reset=True
                )
                self.shown = target
                return
            head, sections, lazy = self.record_view(source_id, record)
        url, sha = self.selection()
        reset = reset or target != self.shown
        self.shown = target
        self.generation += 1
        await inspector.show(head, sections, url=safe_url(url), sha=bool(sha), reset=reset)
        if lazy:
            self.load_children(*lazy, self.generation)

    def can_expand(self, source_id: str, record: dict) -> bool:
        adapter = self.sources[source_id].adapter
        return bool(record.get("children")) or type(adapter).children is not Adapter.children

    def change_view(self, change: Change):
        meta = [clean(change.ref or change.key)]
        if change.sha:
            meta.append(change.sha[:7])
        if change.pr:
            meta.append(f"#{change.pr}")
        if change.owner:
            meta.append(f"@{clean(change.owner)}")
        head = Content.assemble(
            glyph(change.state),
            " ",
            (clean(change.title), "b"),
            "\n",
            muted(f" {widgets.SYMBOLS.dot} ".join(meta)),
            "\n",
            ci_status(change),
            muted(f"  {widgets.SYMBOLS.arrow}  "),
            queue_status(change, with_eta=False),
        )
        checks = change.checks
        ci_rows = [
            Content.assemble(
                glyph(hit.record["state"]),
                " ",
                clean(hit.record["title"]),
                muted(f" {widgets.SYMBOLS.dot} {raw(hit.record)} {widgets.SYMBOLS.dot} "),
                muted(f"{short_age(ago(hit.record.get('created_at')))} ago"),
            )
            for hit in checks
        ] or [dim("no CI runs for this change")]
        lazy = None
        state = change.ci_state
        notable = next((hit for hit in checks if hit.record["state"] == state), None)
        if notable and self.can_expand(notable.source, notable.record):
            lazy = (notable, 0, lines(ci_rows))
            ci_rows = [*ci_rows, dim(f"Loading jobs for {clean(notable.record['title'])}...")]
        noun = "workflow" if len(checks) == 1 else "workflows"
        sections = [
            (Content.assemble(("CI", "b"), muted(f"  {len(checks)} {noun}")), lines(ci_rows)),
            (Content.assemble(("Queue", "b")), self.queue_section(change)),
            (Content.assemble(("History", "b")), self.history_section(change)),
        ]
        links = [
            Content.assemble(muted(f"{label:<6}"), clean(hit.record["url"]))
            for label, hit in (
                ("run", change.runs[0] if change.runs else None),
                ("queue", change.entry),
            )
            if hit and safe_url(hit.record.get("url"))
        ]
        if links:
            sections.append((Content.assemble(("Links", "b")), lines(links)))
        return head, sections, lazy

    def queue_section(self, change: Change) -> Content:
        if change.entry:
            entry = change.entry.record
            waiting = sum(
                r["kind"] == "queue_entry"
                for r in self.snapshots.get(change.entry.source, {}).get("records", [])
            )
            where = (
                f"#{entry['position']} of {waiting}"
                if isinstance(entry.get("position"), int)
                else ""
            )
            rows = [
                Content.assemble(glyph(entry["state"]), f" {raw(entry)}", muted(f"  {where}")),
                dim(f"queued {short_age(ago(entry.get('created_at')))} ago"),
                Content.assemble(
                    muted("ETA  "),
                    (eta_text(eta), "b") if (eta := eta_of(entry)) else muted("unknown"),
                ),
            ]
            note = self.snapshots.get(change.entry.source, {}).get("note")
            if note:
                rows.append(dim(clean(note)))
            return lines(rows)
        if change.attempts:
            attempt = change.attempts[0].record
            took = elapsed(attempt)
            return Content.assemble(
                glyph(attempt["state"]),
                f" {raw(attempt)}",
                muted(f"  took {human_seconds(took)}") if took else "",
            )
        return dim("not in a merge queue")

    def history_section(self, change: Change) -> Content:
        events: list[tuple[float, Content]] = []
        for hit in change.runs:
            record = hit.record
            when = ago(
                record.get("finished_at") or record.get("updated_at") or record.get("created_at")
            )
            events.append(
                (
                    when,
                    Content.assemble(
                        glyph(record["state"]),
                        f" {clean(record['title'])}",
                        muted(f" {raw(record)}"),
                    ),
                )
            )
        for hit in change.entries:
            when = ago(hit.record.get("created_at"))
            events.append((when, Content.assemble(glyph("queued"), " entered the queue")))
        for hit in change.attempts:
            when = ago(hit.record.get("finished_at") or hit.record.get("started_at"))
            events.append(
                (
                    when,
                    Content.assemble(
                        glyph(hit.record["state"]), f" merge attempt {raw(hit.record)}"
                    ),
                )
            )
        events = [e for e in events if e[0] is not None]
        events.sort(key=lambda event: event[0])
        rows = [
            Content.assemble(fit(dim(f"{short_age(when)} ago"), 9), text)
            for when, text in events[:6]
        ]
        return lines(rows) if rows else dim("no events")

    def record_view(self, source_id: str, record: dict):
        head = Content.assemble(glyph(record["state"]), " ", (clean(record["title"]), "b"))
        if record.get("subtitle") and record["subtitle"] != record["title"]:
            head = Content.assemble(head, "\n", muted(clean(record["subtitle"])))
        sections = [(Content.assemble(("Record", "b")), lines(describe(record)))]
        lazy = None
        if self.can_expand(source_id, record):
            sections.append((Content.assemble(("Details", "b")), dim("Loading...")))
            lazy = (Hit(source_id, record), 1, Content(""))
        change = self.by_record.get((source_id, record["id"]))
        if change:
            sections.append(
                (
                    Content.assemble(("Change", "b")),
                    Content.assemble(
                        glyph(change.state),
                        " ",
                        clean(change.ref or change.key),
                        muted("  (Changes tab)"),
                    ),
                )
            )
        note = self.snapshots.get(source_id, {}).get("note")
        if note:
            sections.append((Content.assemble(("Note", "b")), dim(clean(note))))
        return head, sections, lazy

    @work(group="children", exclusive=True)
    async def load_children(self, hit: Hit, index: int, prefix: Content, generation: int) -> None:
        cache_key = (hit.source, hit.record["id"])
        try:
            cached = self.child_cache.get(cache_key)
            if cached and time.monotonic() - cached[0] < CHILD_TTL:
                children = cached[1]
            else:
                children = await self.sources[hit.source].adapter.children(hit.record)
                self.child_cache[cache_key] = (time.monotonic(), children)
                if len(self.child_cache) > 100:
                    self.child_cache.pop(next(iter(self.child_cache)))
            tree = job_tree(children) or [dim("none reported yet")]
        except FETCH_ERRORS as error:
            tree = [dim(f"details unavailable: {clean(str(error) or type(error).__name__)}")]
        if generation != self.generation:
            return
        body = lines([prefix, *tree]) if prefix.plain else lines(tree)
        self.query_one(Inspector).section(index, body)

    # --- events ------------------------------------------------------------ #

    async def on_change_selected(self, message: ChangeSelected) -> None:
        if message.key != self.selected:
            self.selected = message.key
            if self.active_pane() is None:
                await self.show_selection()

    async def on_chip_clicked(self, message: ChipClicked) -> None:
        tabs = self.query_one("#tabs", TabbedContent)
        change = self.by_record.get((message.source, message.record_id))
        if change and change.key in {
            c.key for c in self.changes.values() if self.change_matches(c)
        }:
            self.selected = change.key
            tabs.active = CHANGES
            self.query_one(ChangeTable).highlight(change.key)
            self.query_one("#rows").focus()
            await self.show_selection()
            return
        for name, (source_id, view) in self.panes.items():
            if source_id == message.source and view["kind"] == "queue_entry":
                tabs.active = f"{name}-tab"
                table = self.query_one(f"#{name}", DataTable)
                keys = list(self.records[name])
                if message.record_id in keys:
                    table.move_cursor(row=keys.index(message.record_id), animate=False)
                table.focus()
                await self.show_selection()
                return

    @on(DataTable.RowHighlighted)
    async def row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if not self.rendering and event.data_table.id == self.active_pane():
            await self.show_selection()

    @on(TabbedContent.TabActivated)
    async def tab_changed(self) -> None:
        if self.is_mounted:
            await self.show_selection()

    @on(Input.Changed, "#search")
    async def search_changed(self) -> None:
        await self.rebuild()

    @on(Input.Submitted, "#search")
    def search_submitted(self) -> None:
        self.focus_view()

    @on(Button.Pressed, "#open")
    def open_pressed(self) -> None:
        self.action_open()

    @on(Button.Pressed, "#copy")
    def copy_pressed(self) -> None:
        self.action_copy_sha()

    # --- actions ------------------------------------------------------------ #

    def focus_view(self) -> None:
        name = self.active_pane()
        self.query_one(f"#{name}" if name else "#rows").focus()

    def action_refresh(self) -> None:
        for source_id in self.sources:
            self.fetch(source_id)

    def action_pause(self) -> None:
        self.paused = not self.paused
        self.notify("Paused: no polling" if self.paused else "Resumed", timeout=2)
        if not self.paused:
            self.action_refresh()
        self.render_status()

    def action_search(self) -> None:
        search = self.query_one("#search", Input)
        search.display = True
        search.focus()

    def action_clear(self) -> None:
        search = self.query_one("#search", Input)
        search.value = ""
        search.display = False
        self.focus_view()

    def action_open(self) -> None:
        url, _ = self.selection()
        if safe_url(url):
            webbrowser.open(url)

    def action_copy_sha(self) -> None:
        _, sha = self.selection()
        if sha:
            self.copy_to_clipboard(sha)
            self.notify(clean(sha), title="Copied SHA", timeout=3)

    def action_tab(self, index: int) -> None:
        panes = [pane.id for pane in self.query(TabPane)]
        if index < len(panes):
            self.query_one("#tabs", TabbedContent).active = panes[index]
            self.focus_view()


__all__ = ["QueueWatch"]
