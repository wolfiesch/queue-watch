"""GitHub's native merge queue through the authenticated ``gh`` CLI (GraphQL)."""

from __future__ import annotations

import re
from datetime import UTC, datetime

from .. import process
from ..model import SCHEMA
from . import Adapter

PAGE = 100
STATES = {
    "QUEUED": "queued",
    "AWAITING_CHECKS": "running",
    "MERGEABLE": "queued",
    "LOCKED": "running",
    "UNMERGEABLE": "blocked",
}
QUERY = """
query($owner: String!, $name: String!, $branch: String, $page: Int!) {
  repository(owner: $owner, name: $name) {
    defaultBranchRef { name }
    mergeQueue(branch: $branch) {
      url
      nextEntryEstimatedTimeToMerge
      entries(first: $page) {
        totalCount
        nodes {
          id position state enqueuedAt estimatedTimeToMerge solo jump
          headCommit { oid }
          enqueuer { login }
          pullRequest { number title url headRefName author { login } }
        }
      }
    }
  }
}
"""


async def query(repo: str, branch: str | None) -> dict:
    owner, name = repo.split("/")
    argv = ["gh", "api", "graphql", "-f", f"query={QUERY}", "-F", f"page={PAGE}"]
    argv += ["-f", f"owner={owner}", "-f", f"name={name}"]
    if branch:
        argv += ["-f", f"branch={branch}"]
    data = await process.run_json(argv)
    if data.get("errors"):
        raise RuntimeError("; ".join(error.get("message", "?") for error in data["errors"]))
    repository = data["data"]["repository"]
    if repository is None:
        raise RuntimeError(f"{repo} is not visible to the gh account")
    return repository


async def has_queue(repo: str) -> bool:
    """True when the default branch has a merge queue (used by auto-detection)."""
    return (await query(repo, None)).get("mergeQueue") is not None


class GitHubMergeQueue(Adapter):
    name = "github-merge-queue"
    label = "Merge queue"
    interval = 15.0

    def __init__(self, *, repo: str, branch: str | None = None):
        if not re.fullmatch(r"[\w.-]+/[\w.-]+", repo):
            raise ValueError(f"repo must be owner/repo, got {repo!r}")
        self.repo, self.branch = repo, branch

    async def snapshot(self) -> dict:
        repository = await query(self.repo, self.branch)
        branch = self.branch or (repository.get("defaultBranchRef") or {}).get("name")
        queue = repository.get("mergeQueue")
        records, warnings = [], []
        if queue is None:
            warnings.append(f"{branch} has no merge queue")
        else:
            entries = queue["entries"]
            if entries["totalCount"] > len(entries["nodes"]):
                warnings.append(
                    f"showing first {len(entries['nodes'])} of {entries['totalCount']} entries"
                )
            records = [{**self.record(entry), "repo": self.repo} for entry in entries["nodes"]]
        return {
            "schema": SCHEMA,
            "observed_at": datetime.now(UTC).isoformat(),
            "records": records,
            "views": [
                {
                    "kind": "queue_entry",
                    "label": "Queue",
                    "columns": [
                        {"key": "position", "label": "#", "width": 4},
                        {"key": "state", "label": "State", "width": 15, "format": "state"},
                        {"key": "title", "label": "Pull request", "width": 32},
                        {"key": "owner", "label": "Author", "width": 14},
                        {"key": "created_at", "label": "Queued", "width": 9, "format": "age"},
                        {
                            "key": "fields.estimated_seconds",
                            "label": "ETA",
                            "width": 9,
                            "format": "duration",
                        },
                    ],
                }
            ],
            "warnings": warnings,
            "note": f"Merge queue for {branch}. Positions and estimates come from GitHub.",
        }

    @staticmethod
    def record(entry: dict) -> dict:
        pull = entry.get("pullRequest") or {}
        number = pull.get("number")
        title = pull.get("title") or "pull request"
        return {
            "id": entry["id"],
            "kind": "queue_entry",
            "state": STATES.get(entry.get("state"), "unknown"),
            "raw_state": str(entry.get("state") or "unknown").lower(),
            "title": f"#{number} {title}" if number else title,
            "ref": pull.get("headRefName"),
            "sha": (entry.get("headCommit") or {}).get("oid"),
            "owner": (pull.get("author") or {}).get("login"),
            "position": entry.get("position"),
            "created_at": iso(entry.get("enqueuedAt")),
            "url": pull.get("url"),
            "fields": {
                "pull_request": number,
                "estimated_seconds": entry.get("estimatedTimeToMerge"),
                "enqueuer": (entry.get("enqueuer") or {}).get("login"),
                "solo": entry.get("solo"),
                "jump": entry.get("jump"),
            },
        }


def iso(value: str | None) -> str | None:
    return datetime.fromisoformat(value).isoformat() if value else None
