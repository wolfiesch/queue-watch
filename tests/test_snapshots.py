"""Layout snapshots from the design fixture, frozen at its observed_at.

Update after an intended visual change with ``uv run pytest --snapshot-update``
and review the SVGs under ``tests/__snapshots__`` before committing.
"""

from __future__ import annotations

import copy
import json
import time
from datetime import datetime
from pathlib import Path

import pytest

from queue_watch import widgets
from queue_watch.adapters import Adapter
from queue_watch.app import QueueWatch
from queue_watch.config import Source

FIXTURE = json.loads((Path(__file__).parents[1] / "design/fixtures/demo.json").read_text())
SIZES = {"wide": (160, 45), "narrow": (100, 30), "short": (160, 20)}


class Replay(Adapter):
    name = "replay"
    label = "Replay"

    def __init__(self, snapshot: dict) -> None:
        self.recorded = snapshot

    async def snapshot(self) -> dict:
        return copy.deepcopy(self.recorded)


def replay_sources() -> list[Source]:
    return [
        Source(source_id, entry["label"], 3600, Replay(entry["snapshot"]))
        for source_id, entry in FIXTURE["sources"].items()
    ]


@pytest.fixture(autouse=True)
def frozen_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    observed = datetime.fromisoformat(FIXTURE["observed_at"]).timestamp()
    monkeypatch.setattr(widgets, "now", lambda: observed)
    # Snapshots record colour; a developer's NO_COLOR must not change them.
    monkeypatch.delenv("NO_COLOR", raising=False)


async def settled(pilot) -> None:
    """Every source answered, every rebuild and lazy detail load finished, ages at zero."""
    app = pilot.app
    for _ in range(200):
        await pilot.pause(0.02)
        if len(app.updated) == len(app.sources):
            break
    await app.workers.wait_for_complete()
    await pilot.pause()
    for source_id in app.updated:
        app.updated[source_id] = time.monotonic()
    app.render_status()
    await pilot.pause()


@pytest.mark.parametrize("size", SIZES)
def test_layout(snap_compare, size: str) -> None:
    assert snap_compare(QueueWatch(replay_sources()), terminal_size=SIZES[size], run_before=settled)
