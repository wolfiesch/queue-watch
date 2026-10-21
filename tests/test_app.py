"""The dashboard against fake sources: correlated changes, queue chips, chip clicks."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from textual.content import Content
from textual.widgets import OptionList, TabbedContent

from queue_watch.adapters import Adapter
from queue_watch.app import QueueWatch
from queue_watch.config import Source
from queue_watch.widgets import Inspector, QueueChip, change_columns


def iso(minutes_ago: float) -> str:
    return (datetime.now(UTC) - timedelta(minutes=minutes_ago)).isoformat()


class Fake(Adapter):
    name = "fake"
    label = "Fake"

    def __init__(self, records: list[dict], views: list[dict] | None = None) -> None:
        self.records, self.views = records, views or []

    async def snapshot(self) -> dict:
        return {
            "schema": "queue-watch.snapshot/1",
            "observed_at": iso(0),
            "records": self.records,
            "views": self.views,
        }


class Down(Adapter):
    name = "down"
    label = "Down"

    async def snapshot(self) -> dict:
        raise RuntimeError("connection refused")


CI = [
    {
        "id": "r1",
        "kind": "run",
        "state": "failure",
        "title": "test",
        "ref": "fix-a",
        "sha": "a" * 40,
        "created_at": iso(5),
    },
    {
        "id": "r2",
        "kind": "run",
        "state": "success",
        "title": "lint",
        "ref": "fix-a",
        "sha": "a" * 40,
        "created_at": iso(5),
    },
    {
        "id": "r3",
        "kind": "run",
        "state": "running",
        "title": "test",
        "ref": "feat-b",
        "sha": "b" * 40,
        "created_at": iso(2),
    },
]
QUEUE = [
    {
        "id": "q1",
        "kind": "queue_entry",
        "state": "running",
        "title": "#12 Fix A",
        "ref": "fix-a",
        "position": 1,
        "created_at": iso(10),
    },
    {
        "id": "q2",
        "kind": "queue_entry",
        "state": "queued",
        "title": "Unrelated",
        "ref": "other",
        "position": 2,
        "created_at": iso(3),
    },
]


def sources() -> list[Source]:
    return [
        Source("ci", "CI", 60, Fake(CI)),
        Source("mq", "Merge queue", 60, Fake(QUEUE)),
    ]


async def loaded(pilot) -> None:
    """Wait until every source has answered and its rebuild has finished."""
    app = pilot.app
    for _ in range(100):
        await pilot.pause(0.02)
        if len(app.updated) == len(app.sources):
            # updated is set before the fetch worker rebuilds the view.
            await app.workers.wait_for_complete()
            await pilot.pause()
            return
    raise AssertionError("sources never answered")


async def answered(pilot) -> None:
    """Wait until every source has answered or failed and the view has caught up."""
    app = pilot.app
    for _ in range(100):
        await pilot.pause(0.02)
        if all(s in app.updated or s in app.errors for s in app.sources):
            await app.workers.wait_for_complete()
            await pilot.pause()
            return
    raise AssertionError("sources never settled")


def test_empty_states_say_why_nothing_is_shown() -> None:
    note = {"id": "n1", "kind": "note", "state": "success", "title": "No branch here"}
    merged = {
        "id": "a1",
        "kind": "attempt",
        "state": "success",
        "title": "#9",
        "finished_at": iso(12),
    }
    queue_view = {"kind": "queue_entry", "label": "Queue", "columns": [{"key": "title"}]}
    cases = [
        ([Source("a", "A", 60, Down()), Source("b", "B", 60, Down())], "No source answered."),
        ([Source("a", "A", 60, Fake([]))], "Every source answered with no records."),
        ([Source("a", "A", 60, Fake([note]))], "None of the 1 record names a branch"),
        ([Source("mq", "MQ", 60, Fake([merged], [queue_view]))], "nothing waiting"),
    ]

    async def run(given: list[Source], expected: str) -> None:
        app = QueueWatch(given)
        async with app.run_test(size=(160, 45)) as pilot:
            await answered(pilot)
            if "mq" in app.strips:
                title = str(app.strips["mq"].query_one(".queue-title").render())
                assert title == "MQ  nothing waiting · last merge 12m ago"
                # With no changes the app opens the first source tab: the empty queue.
                assert app.query_one("#tabs", TabbedContent).active != "changes-tab"
                head = str(app.query_one("#insp-head").render())
                assert head == "MQ has sent no rows for this view yet."
                return
            rows = app.query_one("#rows", OptionList)
            assert str(rows.get_option_at_index(0).prompt).startswith(expected)

    for given, expected in cases:
        asyncio.run(run(given, expected))


def test_search_with_no_hits_names_the_query() -> None:
    async def run() -> None:
        app = QueueWatch(sources())
        async with app.run_test(size=(120, 40)) as pilot:
            await loaded(pilot)
            await pilot.press("slash", *"zzz")
            await pilot.pause()
            rows = app.query_one("#rows", OptionList)
            assert (
                str(rows.get_option_at_index(0).prompt) == 'No changes match "zzz". esc to clear.'
            )

    asyncio.run(run())


def test_changes_correlate_across_sources_and_chips_select_them() -> None:
    async def run() -> None:
        app = QueueWatch(sources())
        async with app.run_test(size=(160, 45)) as pilot:
            await loaded(pilot)
            change = app.changes["fix-a"]
            # One change joins both CI runs and the queue entry by branch.
            assert {hit.record["id"] for hit in change.runs} == {"r1", "r2"}
            assert change.entry.record["id"] == "q1"
            assert change.group == "attention" and change.pr == 12
            assert set(app.changes) == {"fix-a", "feat-b", "other"}

            rows = app.query_one("#rows", OptionList)
            ids = [rows.get_option_at_index(i).id for i in range(rows.option_count)]
            assert ids.index("c:fix-a") < ids.index("c:feat-b") < ids.index("c:other")

            # Clicking the second chip selects its change and shows it in the inspector.
            chips = list(app.query(QueueChip))
            assert chips[0].holder and chips[0].record["id"] == "q1"
            await pilot.click(chips[1])
            await pilot.pause()
            assert app.selected == "other"
            assert app.query_one("#tabs", TabbedContent).active == "changes-tab"
            head = app.query_one("#insp-head").render()
            assert "Unrelated" in str(head)

    asyncio.run(run())


def test_search_filters_changes_and_source_tables() -> None:
    async def run() -> None:
        app = QueueWatch(sources())
        async with app.run_test(size=(120, 40)) as pilot:
            await loaded(pilot)
            await pilot.press("slash", *"feat")
            await pilot.pause()
            rows = app.query_one("#rows", OptionList)
            ids = [rows.get_option_at_index(i).id for i in range(rows.option_count)]
            assert [i for i in ids if i.startswith("c:")] == ["c:feat-b"]
            assert app.selected == "feat-b"
            assert all(len(records) <= 1 for records in app.records.values())
            await pilot.press("escape")
            await pilot.pause()
            assert len(app.changes) == 3 and not app.query_one("#search").display
            assert app.query_one(Inspector).query_one("#copy").disabled is False

    asyncio.run(run())


def test_queue_depth_is_sampled_once_per_fetch() -> None:
    async def run() -> None:
        app = QueueWatch(sources())
        async with app.run_test(size=(160, 45)) as pilot:
            await loaded(pilot)
            strip = app.strips["mq"]
            assert list(strip.depth) == [len(strip.entries)]
            assert not strip.has_class("-depth")
            # Redraws such as a search keystroke are not fetches and add no sample.
            await pilot.press("slash", "x", "escape")
            await pilot.pause()
            assert len(strip.depth) == 1
            await pilot.press("r")
            await loaded(pilot)
            assert len(strip.depth) == 2 and strip.has_class("-depth")

    asyncio.run(run())


def test_change_rows_exactly_fill_the_table_at_every_width() -> None:
    # Wider rows wrap in the OptionList; narrower ones leave the AGE column unaligned.
    for comfortable in (False, True):
        for queue in (True, False):
            for width in range(35, 200):
                cols = change_columns(width, queue, comfortable)
                assert sum(c.width for c in cols) + len(cols) - 1 == width, (width, queue)
                assert cols[-1].key == "age"
                if comfortable:
                    assert not {"pr", "title", "owner"} & {c.key for c in cols}


def test_density_toggle_moves_title_owner_and_pr_to_a_second_line() -> None:
    async def run() -> None:
        app = QueueWatch(sources(), density="comfortable")
        async with app.run_test(size=(160, 45)) as pilot:
            await loaded(pilot)
            rows = app.query_one("#rows", OptionList)
            prompt = rows.get_option("c:fix-a").prompt
            first, second = str(prompt).split("\n")
            assert "#12" not in first and "TITLE" not in str(app.query_one("#colhead").render())
            assert second.strip() == "Fix A · #12"
            assert len(second) == len(first)
            await pilot.press("d")
            assert "\n" not in str(rows.get_option("c:fix-a").prompt)
            assert "TITLE" in str(app.query_one("#colhead").render())

    asyncio.run(run())


def test_markup_in_adapter_strings_is_shown_literally() -> None:
    hostile = [
        {
            "id": "h1",
            "kind": "queue_entry",
            "state": "running",
            "title": "handle [/dev] paths",
            "raw_state": "[/x]",
            "ref": "h",
            "sha": "[@click=app.quit]x[/]",
        },
        {
            "id": "h2",
            "kind": "queue_entry",
            "state": "queued",
            "title": "[b]two[/b]",
            "ref": "h2",
            "position": 1,
        },
    ]

    class Hostile(Fake):
        async def snapshot(self) -> dict:
            snapshot = await super().snapshot()
            view = {"kind": "queue_entry", "label": "[/oops]", "columns": [{"key": "title"}]}
            return {**snapshot, "views": [view]}

    async def run() -> None:
        app = QueueWatch([Source("mq", "MQ", 60, Hostile(hostile))])
        async with app.run_test(size=(140, 40)) as pilot:
            for _ in range(100):
                await pilot.pause(0.02)
                if len(app.query(QueueChip)) == 2:
                    break
            chips = list(app.query(QueueChip))
            # Read back as escaped markup: the brackets are text, not a closing tag.
            assert chips[0].border_title == Content("[/x] · next").markup
            await pilot.hover(chips[0])
            await pilot.click(chips[0])
            await pilot.press("c")
            await pilot.pause(0.6)  # tooltip delay
            assert app.is_running and app.selected == "h"
            assert "[/oops]" in str(app.query_one("#tabs").get_tab("pane0-tab").label)

    asyncio.run(run())
