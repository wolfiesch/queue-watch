"""Adapters turn one external system into snapshot v1 (see queue_watch.model)."""

from __future__ import annotations

from typing import ClassVar


class Adapter:
    """Base class. Subclasses take their options as keyword-only arguments."""

    name: ClassVar[str]
    label: ClassVar[str]
    interval: ClassVar[float] = 15.0

    async def snapshot(self) -> dict:
        raise NotImplementedError

    async def children(self, record: dict) -> list[dict]:
        """Detail records (jobs, steps) for the inspector; inline by default."""
        return record.get("children") or []


def registry() -> dict[str, type[Adapter]]:
    from .flock_queue import FlockQueue
    from .github_actions import GitHubActions

    return {adapter.name: adapter for adapter in (GitHubActions, FlockQueue)}
