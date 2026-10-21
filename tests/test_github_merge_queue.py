"""GitHub merge queue state mapping, truncation, and branches without a queue."""

from __future__ import annotations

import asyncio

from queue_watch.adapters import github_merge_queue
from queue_watch.adapters.github_merge_queue import GitHubMergeQueue, has_queue
from queue_watch.model import validate_snapshot


def entry(position, state, **overrides):
    return {
        "id": f"MQE_{position}",
        "position": position,
        "state": state,
        "enqueuedAt": "2026-10-20T10:00:00Z",
        "estimatedTimeToMerge": 600 * position,
        "solo": False,
        "jump": False,
        "headCommit": {"oid": f"{position}" * 40},
        "enqueuer": {"login": "merger"},
        "pullRequest": {
            "number": 100 + position,
            "title": f"Change {position}",
            "url": f"https://github.com/o/r/pull/{100 + position}",
            "headRefName": f"topic-{position}",
            "author": {"login": "author"},
        },
        **overrides,
    }


def respond(monkeypatch, queue, seen=None):
    async def response(argv):
        if seen is not None:
            seen.append(argv)
        return {"data": {"repository": {"defaultBranchRef": {"name": "main"}, "mergeQueue": queue}}}

    monkeypatch.setattr(github_merge_queue.process, "run_json", response)


def test_entries_map_to_contract_states_in_queue_order(monkeypatch):
    nodes = [
        entry(1, "LOCKED"),
        entry(2, "AWAITING_CHECKS"),
        entry(3, "MERGEABLE"),
        entry(4, "QUEUED", headCommit=None),
        entry(5, "UNMERGEABLE"),
        entry(6, "SOMETHING_NEW"),
    ]
    respond(monkeypatch, {"url": "u", "entries": {"totalCount": 7, "nodes": nodes}})
    snapshot = validate_snapshot(asyncio.run(GitHubMergeQueue(repo="o/r").snapshot()))
    assert [(r["position"], r["state"], r["raw_state"]) for r in snapshot["records"]] == [
        (1, "running", "locked"),
        (2, "running", "awaiting_checks"),
        (3, "queued", "mergeable"),
        (4, "queued", "queued"),
        (5, "blocked", "unmergeable"),
        (6, "unknown", "something_new"),
    ]
    first = snapshot["records"][0]
    assert first["title"] == "#101 Change 1"
    assert first["created_at"] == "2026-10-20T10:00:00+00:00"
    assert snapshot["records"][3]["sha"] is None
    assert snapshot["warnings"] == ["showing first 6 of 7 entries"]
    assert "main" in snapshot["note"]


def test_branch_without_queue_is_a_warning_not_an_error(monkeypatch):
    seen = []
    respond(monkeypatch, None, seen)
    snapshot = validate_snapshot(
        asyncio.run(GitHubMergeQueue(repo="o/r", branch="release").snapshot())
    )
    assert snapshot["records"] == []
    assert snapshot["warnings"] == ["release has no merge queue"]
    assert "branch=release" in seen[0]
    assert asyncio.run(has_queue("o/r")) is False
