"""GitHub Actions workflow runs through the authenticated ``gh`` CLI."""

from __future__ import annotations

import asyncio
import json
import re
from datetime import UTC, datetime

from .. import process
from ..model import SCHEMA
from . import Adapter

ACTIVE_STATUSES = ("queued", "in_progress", "waiting", "pending", "requested")
STATES = {
    "queued": "queued",
    "pending": "queued",
    "requested": "queued",
    "waiting": "blocked",
    "action_required": "blocked",
    "in_progress": "running",
    "success": "success",
    "failure": "failure",
    "timed_out": "failure",
    "startup_failure": "failure",
    "cancelled": "cancelled",
    "skipped": "neutral",
    "neutral": "neutral",
    "stale": "neutral",
}
PAGE = 100
PULLS = 100  # recently updated pull requests indexed per refresh, to name merged ones
ETAGS = 200  # cached responses kept for revalidation: the run lists plus recent job lists


def state_of(item: dict) -> tuple[str, str]:
    raw = item.get("conclusion") or item.get("status") or "unknown"
    return STATES.get(raw, "unknown"), raw


def pull_request_of(run: dict) -> int | None:
    """The pull request a run belongs to; a push to a branch has none."""
    for pull in run.get("pull_requests") or []:
        number = pull.get("number")
        if isinstance(number, int):
            return number
    return None


class GitHubActions(Adapter):
    name = "github-actions"
    label = "CI"
    interval = 15.0

    def __init__(self, *, repo: str):
        if not re.fullmatch(r"[\w.-]+/[\w.-]+", repo):
            raise ValueError(f"repo must be owner/repo, got {repo!r}")
        self.repo = repo
        self.etags: dict[str, tuple[str, object, bool]] = {}

    async def get(self, endpoint: str) -> tuple[object, bool]:
        """GET one page, sending back the last ETag; returns ``(body, more_pages)``.

        GitHub does not count a 304 against the rate limit, so a page that has not
        changed since the last poll costs nothing. The cache is per page: a 304 says
        nothing about later pages, so callers that need them still fetch them.
        """
        argv = ["gh", "api", "--include", endpoint]
        cached = self.etags.get(endpoint)
        if cached:
            argv += ["--header", f"If-None-Match: {cached[0]}"]
        returncode, stdout, stderr = await process.capture(argv)
        head, _, body = stdout.decode(errors="replace").replace("\r\n", "\n").partition("\n\n")
        status, *lines = head.split("\n")
        if cached and status.split()[1:2] == ["304"]:
            if endpoint in self.etags:  # evictions can run while this request was out
                self.etags[endpoint] = self.etags.pop(endpoint)  # most recently used last
            return cached[1], cached[2]
        if returncode:
            raise process.failure(argv, returncode, stderr)
        headers = {}
        for line in lines:
            name, sep, value = line.partition(":")
            if sep:
                headers[name.strip().lower()] = value.strip()
        data = json.loads(body)
        more = 'rel="next"' in headers.get("link", "")
        self.etags.pop(endpoint, None)
        if headers.get("etag"):
            self.etags[endpoint] = (headers["etag"], data, more)
            while len(self.etags) > ETAGS:
                self.etags.pop(next(iter(self.etags)))
        return data, more

    async def pull_numbers(self) -> dict[str, int]:
        """Pull request numbers by head commit, for the recently updated ones.

        GitHub empties a run's ``pull_requests`` once the pull request closes, so a
        merged branch would lose its number; this list keeps it. Head commits are
        exact, unlike branch names that get reused.
        """
        endpoint = f"repos/{self.repo}/pulls?state=all&sort=updated&direction=desc&per_page={PULLS}"
        page, _ = await self.get(endpoint)
        numbers: dict[str, int] = {}
        for pull in page if isinstance(page, list) else ():
            number, head = pull.get("number"), pull.get("head") or {}
            if isinstance(number, int) and (commit := head.get("sha")):
                numbers.setdefault(commit, number)
        return numbers

    async def snapshot(self) -> dict:
        # Fetch each active state separately: an old queued run must not disappear
        # behind a page of newer completed runs. GitHub caps each search at 1000.
        async def fetch(status):
            endpoint = f"repos/{self.repo}/actions/runs?per_page={PAGE}"
            if status:
                endpoint += "&status=" + status
            page, _ = await self.get(endpoint)
            return page

        statuses = (*ACTIVE_STATUSES, None)
        *pages, pulls = await asyncio.gather(
            *(fetch(status) for status in statuses), self.pull_numbers()
        )
        runs: dict[int, dict] = {}
        warnings = []
        for status, page in zip(statuses, pages, strict=True):
            if status and page.get("total_count", 0) > PAGE:
                warnings.append(f"{status}: showing first {PAGE} of {page['total_count']} runs")
            for run in page["workflow_runs"]:
                previous = runs.get(run["id"])
                identity = (run.get("run_attempt") or 0, run.get("updated_at") or "")
                if previous is None or identity >= (
                    previous.get("run_attempt") or 0,
                    previous.get("updated_at") or "",
                ):
                    runs[run["id"]] = run
        ordered = sorted(
            runs.values(),
            key=lambda run: (run.get("status") == "completed", -int(run["id"])),
        )
        return {
            "schema": SCHEMA,
            "observed_at": datetime.now(UTC).isoformat(),
            "records": [{**self.record(run, pulls), "repo": self.repo} for run in ordered],
            "views": [
                {
                    "kind": "run",
                    "label": "Runs",
                    "columns": [
                        {"key": "state", "label": "State", "width": 12, "format": "state"},
                        {"key": "fields.pull_request", "label": "PR", "width": 7, "format": "pr"},
                        {"key": "title", "label": "Workflow / title", "width": 25},
                        {"key": "ref", "label": "Branch", "width": 18},
                        {"key": "sha", "label": "SHA", "width": 8, "format": "sha"},
                        {"key": "created_at", "label": "Age", "width": 9, "format": "age"},
                    ],
                }
            ],
            "warnings": warnings,
            "note": f"Up to {PAGE} runs per active state plus the {PAGE} most recent. PR "
            f"numbers also come from the {PULLS} most recently updated pull requests.",
        }

    @staticmethod
    def record(run: dict, pulls: dict[str, int] | None = None) -> dict:
        state, raw = state_of(run)
        pull = pull_request_of(run)
        if pull is None:
            pull = (pulls or {}).get(run.get("head_sha") or "")
        fields = {"run": run["id"], "attempt": run.get("run_attempt")}
        if pull is not None:
            fields["pull_request"] = pull
        return {
            "id": str(run["id"]),
            "kind": "run",
            "state": state,
            "raw_state": raw,
            "title": run.get("name") or run.get("display_title") or str(run["id"]),
            "subtitle": run.get("display_title"),
            "ref": run.get("head_branch"),
            "sha": run.get("head_sha"),
            "created_at": iso(run.get("created_at")),
            "updated_at": iso(run.get("updated_at")),
            "url": run.get("html_url"),
            "fields": fields,
        }

    async def children(self, record: dict) -> list[dict]:
        endpoint = f"repos/{self.repo}/actions/runs/{int(record['id'])}/jobs?per_page={PAGE}"
        first, more = await self.get(endpoint)
        pages = [first]
        if more:
            # Paginate so large platform matrices are not silently truncated.
            pages = await process.run_json(["gh", "api", "--paginate", "--slurp", endpoint])
        jobs = []
        for job in (job for page in pages for job in page["jobs"]):
            state, raw = state_of(job)
            steps = []
            for step in job.get("steps") or []:
                step_state, step_raw = state_of(step)
                steps.append(
                    {
                        "id": f"{job['id']}:{step.get('number')}",
                        "kind": "step",
                        "state": step_state,
                        "raw_state": step_raw,
                        "title": step.get("name") or "step",
                        "started_at": iso(step.get("started_at")),
                        "finished_at": iso(step.get("completed_at")),
                    }
                )
            jobs.append(
                {
                    "id": str(job["id"]),
                    "kind": "job",
                    "state": state,
                    "raw_state": raw,
                    "title": job.get("name") or str(job["id"]),
                    "started_at": iso(job.get("started_at")),
                    "finished_at": iso(job.get("completed_at")),
                    "url": job.get("html_url"),
                    "fields": {"runner": job.get("runner_name")},
                    "children": steps,
                }
            )
        return jobs


def iso(value: str | None) -> str | None:
    """GitHub returns ``Z`` suffixes; normalize to an explicit offset."""
    if not value:
        return None
    return datetime.fromisoformat(value).isoformat()
