"""GitHub Actions paging, precedence, revalidation and state normalization; subprocess limits."""

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


def http(body, *, etag=None, link=None) -> tuple[int, bytes, bytes]:
    """What ``gh api --include`` prints for a 200."""
    head = ["HTTP/2.0 200 OK"] + [f"Etag: {etag}"] * bool(etag) + [f"Link: {link}"] * bool(link)
    return 0, ("\r\n".join(head) + "\r\n\r\n" + json.dumps(body)).encode(), b""


def not_modified(etag) -> tuple[int, bytes, bytes]:
    """``gh api --include`` on a 304: headers only, and a nonzero exit."""
    return 1, f"HTTP/2.0 304 Not Modified\r\nEtag: {etag}\r\n\r\n".encode(), b"gh: HTTP 304\n"


def endpoint_of(argv) -> str:
    return next(arg for arg in argv if arg.startswith("repos/"))


def test_old_active_runs_survive_recent_page_and_newer_state_wins(monkeypatch):
    async def response(argv):
        return http(body(endpoint_of(argv)))

    def body(endpoint):
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

    monkeypatch.setattr(github_actions.process, "capture", response)
    snapshot = validate_snapshot(asyncio.run(GitHubActions(repo="owner/repo").snapshot()))
    assert [(r["id"], r["state"], r["raw_state"]) for r in snapshot["records"]] == [
        ("5", "blocked", "waiting"),
        ("1", "queued", "queued"),
        ("10", "failure", "timed_out"),
    ]
    assert snapshot["warnings"] == ["queued: showing first 100 of 101 runs"]


def test_runs_carry_the_pull_request_they_belong_to(monkeypatch):
    runs = [
        {
            "id": 21,
            "status": "completed",
            "conclusion": "success",
            "name": "CI",
            "head_branch": "release/wolf-0.2.7",
            "pull_requests": [{"number": 284}],
        },
        {
            "id": 22,
            "status": "completed",
            "conclusion": "success",
            "name": "CI",
            "head_branch": "main",
            "pull_requests": [],
        },
    ]

    async def response(argv):
        return http({"total_count": 2, "workflow_runs": runs})

    monkeypatch.setattr(github_actions.process, "capture", response)
    snapshot = validate_snapshot(asyncio.run(GitHubActions(repo="owner/repo").snapshot()))
    fields = {record["id"]: record["fields"] for record in snapshot["records"]}
    assert fields["21"]["pull_request"] == 284
    assert "pull_request" not in fields["22"]


def test_a_merged_pull_request_still_names_its_runs(monkeypatch):
    run = {
        "id": 31,
        "status": "completed",
        "conclusion": "success",
        "name": "CI",
        "head_branch": "fix/x",
        "head_sha": "abc123",
        "pull_requests": [],  # GitHub empties this once the pull request closes
    }
    pulls = [
        {"number": 285, "head": {"sha": "abc123", "ref": "fix/x"}},
        {"number": 284, "head": {"sha": "def456", "ref": "release/1"}},
    ]

    async def response(argv):
        endpoint = endpoint_of(argv)
        if "/pulls?" in endpoint:
            return http(pulls)
        return http({"total_count": 1, "workflow_runs": [run]})

    monkeypatch.setattr(github_actions.process, "capture", response)
    snapshot = validate_snapshot(asyncio.run(GitHubActions(repo="owner/repo").snapshot()))
    assert [record["fields"]["pull_request"] for record in snapshot["records"]] == [285]


def test_jobs_become_child_records_with_steps_across_pages(monkeypatch):
    async def first_page(argv):
        if 'If-None-Match: W/"j"' in argv:
            return not_modified('W/"j"')
        return http(
            {"jobs": []}, etag='W/"j"', link='<https://api.github.com/x?page=2>; rel="next"'
        )

    async def response(argv):
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

    monkeypatch.setattr(github_actions.process, "capture", first_page)
    monkeypatch.setattr(github_actions.process, "run_json", response)
    adapter = GitHubActions(repo="owner/repo")
    for _ in range(2):  # the second first-page request answers 304
        jobs = asyncio.run(adapter.children({"id": "3"}))
        # A 304 says nothing about later pages, so every page is still fetched.
        assert [(j["title"], j["state"], j["fields"]["runner"]) for j in jobs] == [
            ("test", "running", "linux-1"),
            ("lint", "neutral", None),
        ]
    assert [(s["title"], s["state"]) for s in jobs[0]["children"]] == [("Run", "failure")]


def test_unchanged_lists_are_revalidated_and_reuse_the_cached_body(monkeypatch):
    etags = {}
    queued = [{"id": 1, "status": "queued", "updated_at": "2026-10-20T03:00:00Z"}]
    sent = []

    async def response(argv):
        endpoint = endpoint_of(argv)
        header = next((arg for arg in argv if arg.startswith("If-None-Match: ")), None)
        sent.append(header)
        etag = etags.setdefault(endpoint, 'W/"1"')
        if header == f"If-None-Match: {etag}":
            return not_modified(etag)
        runs = queued if endpoint.endswith("status=queued") else []
        # The unfiltered list always has older pages; only its first page is read.
        link = None if "status=" in endpoint else '<https://api.github.com/x?page=2>; rel="next"'
        return http({"total_count": len(runs), "workflow_runs": runs}, etag=etag, link=link)

    monkeypatch.setattr(github_actions.process, "capture", response)
    adapter = GitHubActions(repo="owner/repo")

    def ids():
        return [r["id"] for r in asyncio.run(adapter.snapshot())["records"]]

    assert ids() == ["1"]
    # Six run lists plus the pull request index, none of them cached yet.
    assert sent == [None] * 7
    sent.clear()
    assert ids() == ["1"]  # every list answered 304
    assert all(header and header.startswith("If-None-Match: ") for header in sent)
    etags["repos/owner/repo/actions/runs?per_page=100&status=queued"] = 'W/"2"'
    queued.append({"id": 2, "status": "queued", "updated_at": "2026-10-20T03:01:00Z"})
    assert ids() == ["2", "1"]  # the changed list replaces its cached body


def test_failed_request_raises_gh_message(monkeypatch):
    async def response(argv):
        return 1, b"HTTP/2.0 404 Not Found\r\n\r\n{}", b"gh: Not Found (HTTP 404)\n"

    monkeypatch.setattr(github_actions.process, "capture", response)
    with pytest.raises(RuntimeError, match="Not Found"):
        asyncio.run(GitHubActions(repo="owner/repo").snapshot())


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
