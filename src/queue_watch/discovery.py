"""Where sources come from when no --repo or --config is given.

Order: a repository's own ``.queue-watch.toml`` (only once trusted), then the
GitHub repository of the current checkout (Actions, plus its merge queue when the
default branch has one).

A repository config can run commands and open SSH connections, so a fresh clone
must not be able to do either on its own. ``queue-watch trust`` records the
SHA-256 of the file's exact bytes; any later edit needs trusting again, like
``direnv allow``. Files passed with --config or --profile are the user's own and
are trusted implicitly.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
from pathlib import Path

from .config import ConfigError, Source, build, parse

CONFIG_NAME = ".queue-watch.toml"


def state_dir() -> Path:
    base = os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state"
    return Path(base) / "queue-watch"


def profile_path(name: str) -> Path:
    if not name or "/" in name or name.startswith("."):
        raise ConfigError(f"profile must be a plain name, got {name!r}")
    base = os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config"
    return Path(base) / "queue-watch" / f"{name}.toml"


def git_root(cwd: Path) -> Path | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(cwd), "rev-parse", "--show-toplevel"],
            capture_output=True,
            check=False,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return Path(result.stdout.strip()) if result.returncode == 0 else None


def repo_config(cwd: Path) -> Path | None:
    root = git_root(cwd)
    if root is None:
        return None
    path = root / CONFIG_NAME
    return path if path.is_file() else None


def _store() -> Path:
    return state_dir() / "trusted.json"


def _trusted() -> dict[str, str]:
    try:
        data = json.loads(_store().read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def trust(path: Path) -> list[Source]:
    """Validate ``path`` and record its current bytes as trusted."""
    path = path.resolve()
    data = path.read_bytes()
    sources = parse(data.decode(), path)  # Never trust a file that cannot load.
    trusted = _trusted()
    trusted[str(path)] = digest(data)
    store = _store()
    store.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", dir=store.parent, delete=False) as handle:
        json.dump(trusted, handle, indent=2, sort_keys=True)
    os.replace(handle.name, store)
    return sources


def load_trusted(path: Path) -> list[Source]:
    """Load a repository config only if these exact bytes were trusted."""
    path = path.resolve()
    data = path.read_bytes()  # Check and parse the same bytes.
    recorded = _trusted().get(str(path))
    if recorded != digest(data):
        state = "changed since it was trusted" if recorded else "is not trusted yet"
        raise ConfigError(
            f"{path} {state}. It can run commands and open SSH connections. "
            "Review it, then run: queue-watch trust"
        )
    return parse(data.decode(), path)


def github_repo(cwd: Path) -> str | None:
    try:
        result = subprocess.run(
            ["gh", "repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner"],
            capture_output=True,
            check=False,
            text=True,
            timeout=20,
            cwd=cwd,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    name = result.stdout.strip()
    return name if result.returncode == 0 and name else None


async def detect(cwd: Path) -> list[Source]:
    path = repo_config(cwd)
    if path is not None:
        return load_trusted(path)
    repo = github_repo(cwd)
    if repo is None:
        raise ConfigError(
            "no sources found: not inside a GitHub checkout with an authenticated gh, "
            f"and no {CONFIG_NAME}. Pass --repo, --config or --profile."
        )
    return await github_sources(repo)


async def github_sources(repo: str) -> list[Source]:
    """Actions for ``repo``, plus its merge queue when the default branch has one."""
    from .adapters.github_merge_queue import has_queue

    sources = [build({"id": "ci", "adapter": "github-actions", "repo": repo}, repo)]
    try:
        queued = await has_queue(repo)
    except (OSError, RuntimeError, ValueError, KeyError) as error:
        raise ConfigError(f"cannot check {repo} for a merge queue: {error}") from error
    if queued:
        sources.append(
            build({"id": "merge-queue", "adapter": "github-merge-queue", "repo": repo}, repo)
        )
    return sources
