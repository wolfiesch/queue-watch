"""queue-watch: read-only terminal dashboard for CI runs and merge queues.

Click tabs, rows, filters and toolbar buttons; scroll tables and the inspector
with the wheel. Keyboard: tab/arrows, / search, r refresh, p pause, o open the
selected record, q quit. Times are shown in America/Los_Angeles.

With no source flags, queue-watch uses the repository's .queue-watch.toml once
you have trusted it, and otherwise the current checkout's GitHub repository:
Actions, plus the merge queue when the default branch has one. Nothing is ever
changed: no reruns, cancellations, merges, lock acquisition or remote installs.
Last good data stays visible on failure and is labelled stale. --once prints one
JSON snapshot per source and exits nonzero when any source fails.

Examples:
  queue-watch                               # inside a GitHub checkout
  queue-watch --repo owner/repo
  queue-watch --profile work                # ~/.config/queue-watch/work.toml
  queue-watch --config sources.toml --once
  queue-watch trust                         # after reviewing .queue-watch.toml
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

from . import __version__
from .config import ConfigError, Source, load
from .discovery import CONFIG_NAME, detect, github_sources, profile_path, repo_config, trust


async def once(sources: list[Source]) -> int:
    results = await asyncio.gather(
        *(source.snapshot() for source in sources), return_exceptions=True
    )
    output = {"observed_at": datetime.now(UTC).isoformat(), "sources": {}}
    failed = False
    for source, result in zip(sources, results, strict=True):
        entry = {"adapter": source.adapter.name, "label": source.label}
        if isinstance(result, BaseException):
            entry["error"] = str(result) or type(result).__name__
            failed = True
        else:
            entry["snapshot"] = result
        output["sources"][source.id] = entry
    print(json.dumps(output, indent=2))
    return int(failed)


def trust_command(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="queue-watch trust",
        description=f"Trust a repository's {CONFIG_NAME} after reviewing it. Trust "
        "covers the file's exact bytes; edits need trusting again.",
    )
    parser.add_argument("path", nargs="?", type=Path, help=f"default: the checkout's {CONFIG_NAME}")
    args = parser.parse_args(argv)
    path = args.path or repo_config(Path.cwd())
    if path is None:
        parser.error(f"no {CONFIG_NAME} at the root of this Git checkout")
    try:
        sources = trust(path)
    except (OSError, UnicodeDecodeError, ConfigError) as error:
        parser.error(str(error))
    print(f"Trusted {path.resolve()}")
    for source in sources:
        print(f"  {source.id}: {source.adapter.name} ({source.label})")
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["trust"]:
        return trust_command(argv[1:])
    parser = argparse.ArgumentParser(
        prog="queue-watch",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"queue-watch {__version__}")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--repo", help="GitHub owner/repo: Actions plus any merge queue")
    group.add_argument("--config", type=Path, help="TOML file with [[source]] tables")
    group.add_argument("--profile", help="NAME for ~/.config/queue-watch/NAME.toml")
    parser.add_argument("--once", action="store_true", help="print JSON once; no TUI")
    parser.add_argument(
        "--theme", default="queue-watch", help="Textual theme (default queue-watch: black)"
    )
    parser.add_argument("--ascii", action="store_true", help="ASCII glyphs and borders only")
    parser.add_argument(
        "--density",
        choices=("compact", "comfortable"),
        default="compact",
        help="change rows: compact one-line, or comfortable with title, owner and PR "
        "on a second line (press d to switch)",
    )
    args = parser.parse_args(argv)
    try:
        if args.config:
            sources = load(args.config.expanduser())
        elif args.profile:
            sources = load(profile_path(args.profile))
        elif args.repo:
            sources = asyncio.run(github_sources(args.repo))
        else:
            sources = asyncio.run(detect(Path.cwd()))
    except (ConfigError, OSError, UnicodeDecodeError) as error:
        parser.error(str(error))
    if args.once:
        return asyncio.run(once(sources))
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        parser.error("interactive mode needs a terminal; use --once for JSON")
    from .app import QueueWatch

    app = QueueWatch(sources, theme=args.theme, ascii=args.ascii, density=args.density)
    if args.theme not in app.available_themes:
        parser.error(
            f"unknown theme {args.theme!r}; choose from {', '.join(sorted(app.available_themes))}"
        )
    app.run(mouse=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
