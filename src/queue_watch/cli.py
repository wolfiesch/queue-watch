"""queue-watch: read-only terminal dashboard for CI runs and merge queues.

Click tabs, rows, filters and toolbar buttons; scroll tables and the inspector
with the wheel. Keyboard: tab/arrows, / search, r refresh, p pause, o open the
selected record, q quit. Times are shown in America/Los_Angeles.

Sources come from --repo (GitHub Actions for one repository) or --config (a TOML
file with [[source]] tables; see queue_watch.config). Nothing is ever changed:
no reruns, cancellations, merges, lock acquisition or remote installs. Last good
data stays visible on failure and is labelled stale. --once prints one JSON
snapshot per source and exits nonzero when any source fails.

Examples:
  queue-watch --repo owner/repo
  queue-watch --config ~/.config/queue-watch/work.toml
  queue-watch --repo owner/repo --once
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

from .config import ConfigError, Source, build, load


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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="queue-watch",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--repo", help="GitHub owner/repo; watches its Actions runs")
    group.add_argument("--config", type=Path, help="TOML file with [[source]] tables")
    parser.add_argument("--once", action="store_true", help="print JSON once; no TUI")
    args = parser.parse_args(argv)
    try:
        if args.config:
            sources = load(args.config.expanduser())
        else:
            sources = [
                build({"id": "ci", "adapter": "github-actions", "repo": args.repo}, "--repo")
            ]
    except ConfigError as error:
        parser.error(str(error))
    if args.once:
        return asyncio.run(once(sources))
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        parser.error("interactive mode needs a terminal; use --once for JSON")
    from .app import QueueWatch

    QueueWatch(sources).run(mouse=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
