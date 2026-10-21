"""``--demo``: a made-up repository whose CI and merge queue move on their own.

Nothing touches the network. Simulated time runs at ``SPEED`` times the wall clock
so a minute of watching shows several merges, a failed check and its fix. Events
come from fixed patterns, so every run of the demo tells the same story.
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime

from .adapters import Adapter
from .config import Source
from .model import SCHEMA

SPEED = 15  # simulated seconds per wall-clock second
STEP = 30  # simulated seconds per tick
WARMUP = 40  # ticks simulated before the first frame, so history exists
REPO = "https://github.com/acme/widgets"
NOTE = "Demo data: a simulated repository. Nothing here is real."

PULLS = (
    ("Stream large uploads", "feat/stream-upload", "dana"),
    ("Cache parsed headers", "feat/parser-cache", "lee"),
    ("Document merge queue", "docs/merge-queue", "sam"),
    ("Fix font fallback on Windows", "fix/font-fallback", "ari"),
    ("Faster diff rendering", "feat/diff-speed", "noor"),
    ("Retry flaky socket reads", "fix/socket-retry", "kai"),
    ("Bump lockfile", "chore/lockfile", "lee"),
    ("Add CSV exporter", "feat/csv-export", "dana"),
    ("Tidy error messages", "fix/errors", "sam"),
    ("Theme tokens for dark mode", "feat/theme", "noor"),
)
ARRIVAL_GAPS = (2, 1, 3, 6, 7, 2, 3, 8)  # ticks between arrivals: bursts, then lulls
CHECK_TICKS = (3, 5, 4, 6, 3)  # how long CI takes on a pull request
FAILS_EVERY = 4  # every fourth pull request fails CI once, then gets a fix pushed
FIX_AFTER = 5  # ticks between a failure and the fix being pushed
MERGE_TICKS = (3, 4, 2, 3, 5)  # how long merging takes, by pull request
MERGE_FAILS_EVERY = 6  # every sixth merge attempt fails and leaves the queue


class Clock:
    """Simulated epoch seconds, starting at the wall clock and running ``SPEED`` times faster."""

    def __init__(self) -> None:
        self.start = time.time()
        self.origin = time.monotonic()

    def now(self) -> float:
        return self.start + (time.monotonic() - self.origin) * SPEED


def iso(seconds: float) -> str:
    return datetime.fromtimestamp(seconds, UTC).isoformat(timespec="seconds")


def sha(*parts: object) -> str:
    return hashlib.sha1("-".join(map(str, parts)).encode()).hexdigest()[:10]


@dataclass
class Run:
    id: int
    workflow: str
    title: str
    ref: str
    sha: str
    start: int  # tick
    end: int  # tick the run finishes
    outcome: str  # final state: success or failure

    def state(self, tick: int) -> str:
        return "running" if tick < self.end else self.outcome


@dataclass
class Entry:
    number: int
    title: str
    ref: str
    owner: str
    joined: int
    run: Run
    fails: bool  # CI fails once before a fix lands
    state: str = "checks"  # checks, queued, merging, blocked
    since: int = 0  # tick the current state began

    @property
    def merge_ticks(self) -> int:
        return MERGE_TICKS[self.number % len(MERGE_TICKS)]


@dataclass
class Simulation:
    clock: Clock
    epoch: float = 0.0  # simulated time of tick 0
    tick: int = 0
    pulls: int = 0
    started: int = 0  # runs started, for unique run ids
    merges: int = 0
    next_arrival: int = 0
    queue: list[Entry] = field(default_factory=list)
    runs: list[Run] = field(default_factory=list)
    attempts: list[dict] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.epoch = self.clock.now() - WARMUP * STEP
        self.release = self.start_run("Release", "v2.4.0", "main", 2, 0, "blocked")
        self.advance()

    def at(self, tick: int) -> float:
        return self.epoch + tick * STEP

    def advance(self) -> None:
        target = int((self.clock.now() - self.epoch) // STEP)
        while self.tick < target:
            self.tick += 1
            self.step()

    def start_run(self, workflow: str, title: str, ref: str, ticks: int, salt: int, outcome: str):
        run = Run(
            9100 + self.started, workflow, title, ref, sha(ref, salt), self.tick,
            self.tick + ticks, outcome,
        )  # fmt: skip
        self.started += 1
        self.runs.append(run)
        return run

    def step(self) -> None:
        tick = self.tick
        if tick >= self.next_arrival:
            self.arrive()
        for entry in self.queue:
            if entry.state == "checks" and tick >= entry.run.end:
                entry.state = "blocked" if entry.run.outcome == "failure" else "queued"
                entry.since = tick
            elif entry.state == "blocked" and tick - entry.since >= FIX_AFTER:
                entry.fails = False
                entry.run = self.start_run(
                    "CI", entry.title, entry.ref, CHECK_TICKS[entry.number % len(CHECK_TICKS)],
                    tick, "success",
                )  # fmt: skip
                entry.state, entry.since = "checks", tick
        head = next((e for e in self.queue if e.state == "merging"), None)
        if head and tick - head.since >= head.merge_ticks:
            self.merges += 1
            failed = self.merges % MERGE_FAILS_EVERY == 0
            self.attempts.append(
                {
                    "id": f"attempt-{self.merges}",
                    "kind": "attempt",
                    "state": "failure" if failed else "success",
                    "raw_state": "failed" if failed else "merged",
                    "title": f"#{head.number} {head.title}",
                    "ref": head.ref,
                    "sha": head.run.sha,
                    "owner": head.owner,
                    "started_at": iso(self.at(head.since)),
                    "finished_at": iso(self.at(tick)),
                    "url": f"{REPO}/pull/{head.number}",
                    "fields": {"pull_request": head.number},
                }
            )
            self.queue.remove(head)
            if not failed:
                self.start_run("CI", f"Merge #{head.number}", "main", 3, tick, "success")
            head = None
        if head is None:
            # Merges go in queue order; failed entries wait aside for their fix.
            first = next((e for e in self.queue if e.state != "blocked"), None)
            if first and first.state == "queued":
                first.state, first.since = "merging", tick
        del self.runs[: max(0, len(self.runs) - 14)]
        del self.attempts[: max(0, len(self.attempts) - 40)]  # about an hour of merges
        if self.release not in self.runs:
            self.runs.insert(0, self.release)

    def arrive(self) -> None:
        title, ref, owner = PULLS[self.pulls % len(PULLS)]
        number = 482 + self.pulls
        fails = self.pulls % FAILS_EVERY == FAILS_EVERY - 1
        ticks = CHECK_TICKS[self.pulls % len(CHECK_TICKS)]
        run = self.start_run("CI", title, ref, ticks, self.tick, "failure" if fails else "success")
        self.queue.append(Entry(number, title, ref, owner, self.tick, run, fails, since=self.tick))
        self.next_arrival = self.tick + ARRIVAL_GAPS[self.pulls % len(ARRIVAL_GAPS)]
        self.pulls += 1

    def run_record(self, run: Run) -> dict:
        state = run.outcome if run.outcome == "blocked" else run.state(self.tick)
        lint_end = min(run.start + 1, run.end)
        jobs = []
        for name, start, end, outcome in (
            ("lint", run.start, lint_end, "success"),
            ("test (3.12)", run.start, run.end, run.outcome),
        ):
            done = self.tick >= end and outcome != "blocked"
            job_state = outcome if done else ("queued" if outcome == "blocked" else "running")
            job = {
                "id": f"{run.id}-{name}",
                "kind": "job",
                "state": job_state,
                "raw_state": job_state,
                "title": name,
                "started_at": iso(self.at(start)),
                "fields": {"runner": "ubuntu-24.04"},
            }
            if done:
                job["finished_at"] = iso(self.at(end))
                job["fields"]["duration_seconds"] = (end - start) * STEP
            jobs.append(job)
        return {
            "id": str(run.id),
            "kind": "run",
            "state": state,
            "raw_state": {"running": "in_progress", "blocked": "waiting"}.get(state, state),
            "title": run.workflow,
            "subtitle": run.title,
            "ref": run.ref,
            "sha": run.sha,
            "created_at": iso(self.at(run.start)),
            "updated_at": iso(self.at(min(self.tick, run.end))),
            "url": f"{REPO}/actions/runs/{run.id}",
            "fields": {"run_id": run.id, "attempt": 1},
            "children": jobs,
        }

    def entry_record(self, entry: Entry, position: int, eta: float | None) -> dict:
        state, raw = {
            "checks": ("queued", "awaiting_checks"),
            "queued": ("queued", "queued"),
            "merging": ("running", "merging"),
            "blocked": ("blocked", "unmergeable"),
        }[entry.state]
        fields: dict = {"pull_request": entry.number}
        if eta is not None:
            fields["estimated_seconds"] = round(eta)
        return {
            "id": f"entry-{entry.number}",
            "kind": "queue_entry",
            "state": state,
            "raw_state": raw,
            "title": f"#{entry.number} {entry.title}",
            "ref": entry.ref,
            "sha": entry.run.sha,
            "owner": entry.owner,
            "position": position,
            "created_at": iso(self.at(entry.joined)),
            "url": f"{REPO}/pull/{entry.number}",
            "fields": fields,
        }

    def ci(self) -> dict:
        self.advance()
        records = [self.run_record(run) for run in reversed(self.runs)]
        return {"schema": SCHEMA, "observed_at": iso(self.clock.now()), "records": records}

    def merge_queue(self) -> dict:
        self.advance()
        now = self.clock.now()
        records, ahead = [], 0.0
        for position, entry in enumerate(self.queue, 1):
            eta = None
            if entry.state == "merging":
                ahead = max(0.0, self.at(entry.since + entry.merge_ticks) - now)
                eta = ahead
            elif entry.state != "blocked":
                checks = max(0.0, self.at(entry.run.end) - now)
                ahead = max(ahead, checks) + entry.merge_ticks * STEP
                eta = ahead
            records.append(self.entry_record(entry, position, eta))
        return {
            "schema": SCHEMA,
            "observed_at": iso(now),
            "records": records + self.attempts[::-1],
            "note": NOTE,
        }


class DemoCI(Adapter):
    name = "demo-ci"
    label = "CI"

    def __init__(self, simulation: Simulation) -> None:
        self.simulation = simulation

    async def snapshot(self) -> dict:
        return self.simulation.ci()


class DemoMergeQueue(DemoCI):
    name = "demo-merge-queue"
    label = "Merge queue"

    async def snapshot(self) -> dict:
        return self.simulation.merge_queue()


def sources(clock: Clock) -> list[Source]:
    simulation = Simulation(clock)
    return [
        Source("ci", DemoCI.label, 2, DemoCI(simulation)),
        Source("merge-queue", DemoMergeQueue.label, 2, DemoMergeQueue(simulation)),
    ]
