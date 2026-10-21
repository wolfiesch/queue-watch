import asyncio

import pytest

from queue_watch import demo, widgets
from queue_watch.app import QueueWatch
from queue_watch.changes import correlate
from queue_watch.model import validate_snapshot


class Clock:
    now_value = 1_800_000_000.0

    def now(self) -> float:
        return self.now_value


@pytest.mark.parametrize("repo", [demo.WIDGETS, demo.GADGETS], ids=lambda r: r.name)
def test_demo_queue_keeps_moving_with_valid_snapshots(repo: demo.Repo) -> None:
    clock = Clock()
    simulation = demo.Simulation(clock, repo)
    lengths, merged = [], set()
    for _ in range(400):  # 400 ticks: well past every pattern's period
        clock.now_value += demo.STEP
        queue = validate_snapshot(simulation.merge_queue())
        validate_snapshot(simulation.ci())
        entries = [r for r in queue["records"] if r["kind"] == "queue_entry"]
        assert [e["position"] for e in entries] == list(range(1, len(entries) + 1))
        assert sum(e["raw_state"] == "merging" for e in entries) <= 1
        lengths.append(len(entries))
        merged |= {r["id"] for r in queue["records"] if r["kind"] == "attempt"}
    assert len(merged) > 100
    assert max(lengths) <= 8  # arrivals and merges balance; the queue never runs away


def test_two_repo_demo_keeps_shared_branches_apart() -> None:
    clock = Clock()
    clock.now_value += 400 * demo.STEP  # both repositories have run every pull request
    sources = demo.sources(clock, 2)
    assert len({s.id for s in sources}) == 4
    snapshots = {s.id: validate_snapshot(asyncio.run(s.adapter.snapshot())) for s in sources}
    repos = {(c.repo, c.ref) for c in correlate(snapshots)}
    for ref in ("main", "chore/lockfile", "fix/errors"):
        assert {("acme/widgets", ref), ("acme/gadgets", ref)} <= repos


def test_two_repo_demo_loads_every_queue(monkeypatch: pytest.MonkeyPatch) -> None:
    # Four sources answer together; strips mounted mid-rebuild once crashed the app.
    clock = demo.Clock()
    monkeypatch.setattr(widgets, "now", clock.now)

    async def run() -> None:
        app = QueueWatch(demo.sources(clock, 2))
        async with app.run_test(size=(112, 40)) as pilot:
            for _ in range(100):
                await pilot.pause(0.02)
                if len(app.updated) == len(app.sources):
                    break
            await app.workers.wait_for_complete()
            assert set(app.strips) == {"acme-widgets-merge-queue", "acme-gadgets-merge-queue"}

    asyncio.run(run())
