"""Where sources come from when no --repo or --config is given, and profiles.

Order: a repository's own ``.queue-watch.toml`` (only once trusted), then the
GitHub repository of the current checkout (Actions, plus its merge queue when the
default branch has one), then the ``default`` profile.

Profiles are the user's own TOML files under ``$XDG_CONFIG_HOME/queue-watch``.
``queue-watch profile add NAME --repo owner/repo`` appends sources for each
repository to one, keeping anything already written there.

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
import tomllib
from pathlib import Path

from .config import ConfigError, Source, build, load, parse

CONFIG_NAME = ".queue-watch.toml"
DEFAULT_PROFILE = "default"
PROFILE_HEADER = """\
# queue-watch profile: run with `queue-watch --profile {name}`.
# Add repositories with `queue-watch profile add {name} --repo owner/repo`, or edit
# by hand; https://github.com/wolfiesch/queue-watch lists every adapter's options.
"""


def state_dir() -> Path:
    base = os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state"
    return Path(base) / "queue-watch"


def profile_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config"
    return Path(base) / "queue-watch"


def profile_path(name: str) -> Path:
    if not name or "/" in name or name.startswith("."):
        raise ConfigError(f"profile must be a plain name, got {name!r}")
    return profile_dir() / f"{name}.toml"


def profiles() -> list[Path]:
    return sorted(profile_dir().glob("*.toml"))


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
    if repo is not None:
        return await github_sources(repo)
    default = profile_path(DEFAULT_PROFILE)
    if default.is_file():
        return load(default)
    raise ConfigError(
        "no sources found: not inside a GitHub checkout with an authenticated gh, "
        f"no {CONFIG_NAME} and no default profile. Pass --repo, --config or --profile, "
        "or create one: queue-watch profile add default --repo owner/repo"
    )


async def github_entries(repo: str, prefix: str = "") -> list[dict]:
    """Source tables for Actions on ``repo``, plus its merge queue when the default
    branch has one. ``prefix`` names the repository in ids and labels."""
    from .adapters.github_merge_queue import has_queue

    entries = [{"id": f"{prefix}ci", "adapter": "github-actions", "repo": repo}]
    build(entries[0], repo)  # Reject a malformed repo before asking GitHub.
    try:
        queued = await has_queue(repo)
    except (OSError, RuntimeError, ValueError, KeyError) as error:
        raise ConfigError(f"cannot check {repo} for a merge queue: {error}") from error
    if queued:
        entries.append(
            {"id": f"{prefix}merge-queue", "adapter": "github-merge-queue", "repo": repo}
        )
    if prefix:
        name = repo.split("/")[1]
        entries[0]["label"] = f"{name} CI"
        if queued:
            entries[1]["label"] = f"{name} merge queue"
    return entries


async def github_sources(repo: str) -> list[Source]:
    """Actions for ``repo``, plus its merge queue when the default branch has one."""
    return [build(entry, repo) for entry in await github_entries(repo)]


def toml_table(entry: dict) -> str:
    # JSON strings are valid TOML basic strings.
    return "\n[[source]]\n" + "".join(
        f"{key} = {json.dumps(value)}\n" for key, value in entry.items()
    )


async def add_to_profile(name: str, repos: list[str]) -> tuple[Path, list[str], list[str]]:
    """Append GitHub sources for each new repository in ``repos`` to profile ``name``.

    Returns the profile's path, the repositories added, and those it already had.
    """
    path = profile_path(name)
    text = path.read_text() if path.exists() else PROFILE_HEADER.format(name=name)
    try:
        existing = tomllib.loads(text).get("source") or []
    except tomllib.TOMLDecodeError as error:
        raise ConfigError(f"{path}: {error}") from error
    present = {
        entry["repo"].casefold()
        for entry in existing
        if isinstance(entry, dict) and isinstance(entry.get("repo"), str)
    }
    ids = {entry.get("id") for entry in existing if isinstance(entry, dict)}
    added, skipped = [], []
    for repo in repos:
        if repo.casefold() in present:
            skipped.append(repo)
            continue
        prefix = base = repo.replace("/", "-").lower() + "-"
        number = 2
        while f"{prefix}ci" in ids or f"{prefix}merge-queue" in ids:  # a-b/c and a/b-c
            prefix, number = f"{base}{number}-", number + 1
        entries = await github_entries(repo, prefix)
        text += "".join(map(toml_table, entries))
        ids.update(entry["id"] for entry in entries)
        present.add(repo.casefold())
        added.append(repo)
    if added:
        parse(text, path)  # Never write a profile that cannot load.
        target = path.resolve()  # Keep a symlinked profile (dotfiles) a symlink.
        target.parent.mkdir(parents=True, exist_ok=True)
        mode = target.stat().st_mode & 0o777 if target.exists() else 0o644
        with tempfile.NamedTemporaryFile("w", dir=target.parent, delete=False) as handle:
            handle.write(text)
        os.chmod(handle.name, mode)
        os.replace(handle.name, target)
    return path, added, skipped


def describe_profile(path: Path) -> list[str]:
    """One line per source in a profile, without building any adapter."""
    try:
        entries = tomllib.loads(path.read_text()).get("source") or []
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        return [f"cannot read: {error}"]
    lines = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        target = entry.get("repo") or entry.get("host") or ""
        label = entry.get("label") or entry.get("id") or "?"
        lines.append(f"{label}  ({entry.get('adapter', '?')}{' ' + str(target) if target else ''})")
    return lines or ["no sources"]
