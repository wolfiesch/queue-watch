"""GitHub Actions paging, precedence and state normalization; subprocess limits."""

from __future__ import annotations

import asyncio
import json
import sys
import time

import pytest

from queue_watch import process
from queue_watch.adapters import github_actions
from queue_watch.adapters.github_actions import GitHubActions
from queue_watch.model import validate_snapshot


def test_old_active_runs_survive_recent_page_and_newer_state_wins(monkeypatch):
    async def response(argv):
        endpoint = argv[-1]
        if "status=queued" in endpoint:
            return {
                "total_count": 101,
                "workflow_runs": [
                    {"id": 1, "status": "queued", "updated_at": "2026-10-20T03:00:00Z"}
                ],
            }
        if "status=waiting" in endpoint:
            return {
                "total_count": 1,
                "workflow_runs": [
                    {"id": 5, "status": "waiting", "updated_at": "2026-10-20T03:00:00Z"}
                ],
            }
        if "status=" in endpoint:
            return {"total_count": 0, "workflow_runs": []}
        return {
            "total_count": 300,
            "workflow_runs": [
                {
                    "id": 10,
                    "status": "completed",
                    "conclusion": "timed_out",
                    "updated_at": "2026-10-20T01:00:00Z",
                },
                {
                    "id": 1,
                    "status": "completed",
                    "conclusion": "success",
                    "updated_at": "2026-10-20T02:00:00Z",
                },
            ],
        }

    monkeypatch.setattr(github_actions.process, "run_json", response)
    snapshot = validate_snapshot(asyncio.run(GitHubActions(repo="owner/repo").snapshot()))
    assert [(r["id"], r["state"], r["raw_state"]) for r in snapshot["records"]] == [
        ("5", "blocked", "waiting"),
        ("1", "queued", "queued"),
        ("10", "failure", "timed_out"),
    ]
    assert snapshot["warnings"] == ["queued: showing first 100 of 101 runs"]


def test_jobs_become_child_records_with_steps(monkeypatch):
    async def response(argv):
        assert "--paginate" in argv
        return [
            {
                "jobs": [
                    {
                        "id": 7,
                        "name": "test",
                        "status": "in_progress",
                        "runner_name": "linux-1",
                        "started_at": "2026-10-20T01:00:00Z",
                        "steps": [
                            {
                                "number": 1,
                                "name": "Run",
                                "status": "completed",
                                "conclusion": "failure",
                            }
                        ],
                    }
                ]
            },
            {"jobs": [{"id": 8, "name": "lint", "status": "completed", "conclusion": "skipped"}]},
        ]

    monkeypatch.setattr(github_actions.process, "run_json", response)
    jobs = asyncio.run(GitHubActions(repo="owner/repo").children({"id": "3"}))
    assert [(j["title"], j["state"], j["fields"]["runner"]) for j in jobs] == [
        ("test", "running", "linux-1"),
        ("lint", "neutral", None),
    ]
    assert [(s["title"], s["state"]) for s in jobs[0]["children"]] == [("Run", "failure")]


def test_repo_must_be_owner_slash_name():
    with pytest.raises(ValueError, match="owner/repo"):
        GitHubActions(repo="owner/repo; rm -rf /")


def test_command_timeout_does_not_hang_refresh():
    with pytest.raises(TimeoutError):
        asyncio.run(
            process.run_json([sys.executable, "-c", "import time; time.sleep(60)"], timeout=0.05)
        )


def test_timeout_is_not_held_open_by_child_processes():
    started = time.monotonic()
    with pytest.raises(TimeoutError):
        asyncio.run(process.run_json(["sh", "-c", "sleep 30 & sleep 30"], timeout=0.5))
    assert time.monotonic() - started < 5


def test_invalid_command_output_is_not_an_empty_success():
    with pytest.raises(json.JSONDecodeError):
        asyncio.run(process.run_json([sys.executable, "-c", "print('not json')"]))
