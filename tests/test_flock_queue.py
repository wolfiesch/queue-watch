"""Queue membership boundaries, using an isolated Git checkout and a fake /proc."""

from __future__ import annotations

import asyncio
import os
import subprocess
from pathlib import Path

import pytest

from queue_watch.adapters import flock_queue
from queue_watch.adapters.flock_queue import FlockQueue, flock_probe
from queue_watch.model import validate_snapshot

OPTIONS = {
    "lock": "{git_dir}/merge.lock",
    "tickets": "{git_dir}/merge.queue/*.ticket",
    "process": "merge.py",
    "owner_flag": "--owner",
    "history": "{git_dir}/merge-history.jsonl",
}


class World:
    def __init__(self, root: Path):
        subprocess.run(["git", "init", "-q", str(root / "repo")], check=True)
        self.repo = root / "repo"
        self.git = self.repo / ".git"
        self.queue = self.git / "merge.queue"
        self.queue.mkdir()
        self.proc = root / "proc"
        self.proc.mkdir()
        self.locks: list[str] = []
        (self.proc / "locks").write_text("")

    def process(self, pid: int, *argv: str) -> None:
        directory = self.proc / str(pid)
        directory.mkdir()
        (directory / "cmdline").write_bytes(("\0".join(argv) + "\0").encode())

    def lock(self, path: Path, pid: int, blocked: bool = False) -> None:
        path.touch()
        stat = path.stat()
        identity = f"{os.major(stat.st_dev):02x}:{os.minor(stat.st_dev):02x}:{stat.st_ino}"
        self.locks.append(
            f"1: {'-> ' if blocked else ''}FLOCK ADVISORY WRITE {pid} {identity} 0 EOF"
        )
        (self.proc / "locks").write_text("\n".join(self.locks))

    def probe(self, **extra) -> dict:
        return flock_probe({**OPTIONS, "root": str(self.repo), **extra}, str(self.proc))


@pytest.fixture
def world(tmp_path):
    return World(tmp_path)


def test_fifo_uses_held_tickets_without_deleting_abandoned_files(world):
    world.process(100, "python3", "scripts/merge.py", "first", "--owner", "central")
    world.process(200, "python3", "scripts/merge.py", "second")
    world.lock(world.queue / "0001.ticket", 100)
    world.lock(world.queue / "0003.ticket", 200)
    orphan = world.queue / "0002.ticket"
    orphan.write_text("preserve me")
    world.lock(world.git / "merge.lock", 100)
    world.lock(world.git / "merge.lock", 200, blocked=True)
    rows = world.probe()["queue"]
    assert [(r["item"], r["owner"], r["state"], r["position"]) for r in rows] == [
        ("first", "central", "integrating", 1),
        ("second", None, "queued", 2),
    ]
    assert orphan.read_text() == "preserve me"


def test_launch_wrappers_do_not_duplicate_a_preparing_merger(world):
    args = ("scripts/merge.py", "candidate", "--owner", "central")
    world.process(10, "timeout", "600", "uv", "run", "python3", *args)
    world.process(11, "uv", "run", "python3", *args)
    world.process(12, "python3", *args)
    rows = world.probe()["queue"]
    assert [(r["pid"], r["item"], r["state"]) for r in rows] == [(12, "candidate", "preparing")]


def test_a_merger_executable_matches_without_an_interpreter(world):
    world.process(30, "/usr/local/bin/merge.py", "direct")
    world.process(31, "bash", "-c", "merge.py wrapped")
    rows = world.probe()["queue"]
    assert [(r["pid"], r["item"]) for r in rows] == [(30, "direct")]


def test_holder_without_ticket_has_no_invented_fifo_position(world):
    world.process(100, "python3", "scripts/merge.py", "legacy")
    world.lock(world.git / "merge.lock", 100)
    rows = world.probe()["queue"]
    assert [(r["item"], r["state"], r["position"]) for r in rows] == [
        ("legacy", "integrating", None)
    ]


def test_history_excludes_partial_append_and_reports_corrupt_records(world):
    log = world.git / "merge-history.jsonl"
    log.write_text(
        '{"item":"old","outcome":"merged"}\ninvalid\n'
        '{"item":"new","outcome":"refused"}\n{"item":"partial"'
    )
    probe = world.probe()
    assert [r["item"] for r in probe["history"]] == ["new", "old"]
    assert probe["warnings"] == ["Skipped 1 malformed history records"]
    assert log.read_text().endswith('{"item":"partial"')


def test_git_dir_template_without_root_is_refused(world):
    with pytest.raises(ValueError, match="root"):
        flock_probe(OPTIONS, str(world.proc))


def test_snapshot_normalizes_states_and_keeps_extra_history_fields(world, monkeypatch):
    world.process(100, "python3", "merge.py", "head")
    world.process(200, "python3", "merge.py", "next")
    world.process(300, "python3", "merge.py", "prep")
    world.lock(world.queue / "1.ticket", 100)
    world.lock(world.queue / "2.ticket", 200)
    world.lock(world.git / "merge.lock", 100)
    (world.git / "merge-history.jsonl").write_text(
        '{"item":"a","outcome":"merged","finished_at":"2026-10-20T01:00:00+00:00",'
        '"lock_hold_seconds":12.5}\n'
        '{"item":"b","outcome":"refused","finished_at":"not a time"}\n'
        '{"item":"c","outcome":"surprising"}\n'
    )
    real = flock_probe
    monkeypatch.setattr(flock_queue, "flock_probe", lambda options: real(options, str(world.proc)))
    adapter = FlockQueue(
        **OPTIONS,
        root=str(world.repo),
        history_columns=[{"key": "fields.lock_hold_seconds", "label": "Hold", "format": "seconds"}],
    )
    snapshot = validate_snapshot(asyncio.run(adapter.snapshot()))
    summary = [(r["kind"], r["title"], r["state"], r["raw_state"]) for r in snapshot["records"]]
    assert summary == [
        ("queue_entry", "head", "running", "integrating"),
        ("queue_entry", "next", "queued", "queued"),
        ("queue_entry", "prep", "running", "preparing"),
        ("attempt", "c", "unknown", "surprising"),
        ("attempt", "b", "failure", "refused"),
        ("attempt", "a", "success", "merged"),
    ]
    attempt_a = snapshot["records"][-1]
    assert attempt_a["fields"] == {"lock_hold_seconds": 12.5}
    assert snapshot["records"][-2].get("finished_at") is None
    attempts_view = next(v for v in snapshot["views"] if v["kind"] == "attempt")
    assert attempts_view["columns"][-1]["key"] == "fields.lock_hold_seconds"
