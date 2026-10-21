"""GitHub Actions workflow runs through the authenticated ``gh`` CLI."""

from __future__ import annotations

import asyncio
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


def state_of(item: dict) -> tuple[str, str]:
    raw = item.get("conclusion") or item.get("status") or "unknown"
    return STATES.get(raw, "unknown"), raw


class GitHubActions(Adapter):
    name = "github-actions"
    label = "CI"
    interval = 15.0

    def __init__(self, *, repo: str):
        if not re.fullmatch(r"[\w.-]+/[\w.-]+", repo):
            raise ValueError(f"repo must be owner/repo, got {repo!r}")
        self.repo = repo

    async def snapshot(self) -> dict:
        # Fetch each active state separately: an old queued run must not disappear
        # behind a page of newer completed runs. GitHub caps each search at 1000.
        async def fetch(status):
            endpoint = f"repos/{self.repo}/actions/runs?per_page={PAGE}"
            if status:
                endpoint += "&status=" + status
            return await process.run_json(["gh", "api", endpoint])

        statuses = (*ACTIVE_STATUSES, None)
        pages = await asyncio.gather(*(fetch(status) for status in statuses))
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
            "records": [self.record(run) for run in ordered],
            "views": [
                {
                    "kind": "run",
                    "label": "Runs",
                    "columns": [
                        {"key": "state", "label": "State", "width": 12, "format": "state"},
                        {"key": "title", "label": "Workflow / title", "width": 25},
                        {"key": "ref", "label": "Branch", "width": 18},
                        {"key": "sha", "label": "SHA", "width": 8, "format": "sha"},
                        {"key": "created_at", "label": "Age", "width": 9, "format": "age"},
                    ],
                }
            ],
            "warnings": warnings,
            "note": f"Up to {PAGE} runs per active state plus the {PAGE} most recent.",
        }

    @staticmethod
    def record(run: dict) -> dict:
        state, raw = state_of(run)
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
            "fields": {"run": run["id"], "attempt": run.get("run_attempt")},
        }

    async def children(self, record: dict) -> list[dict]:
        # Paginate so large platform matrices are not silently truncated.
        pages = await process.run_json(
            [
                "gh",
                "api",
                "--paginate",
                "--slurp",
                f"repos/{self.repo}/actions/runs/{int(record['id'])}/jobs?per_page={PAGE}",
            ]
        )
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
