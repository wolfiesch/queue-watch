"""A FIFO of flock-held ticket files guarding one integration lock, on Linux.

The pattern: each waiting merger holds an exclusive flock on its own ticket file
(sorted names give FIFO order) and the head of the queue holds the main lock. The
adapter reads lock holders from ``/proc/locks`` instead of acquiring a flock, so
watching never perturbs the queue. Processes are matched by command line: either
the executable itself is named ``process``, or an interpreter (``interpreters``
prefixes) runs a script named ``process``; the first positional argument after it
names the change. Wrapper processes (``uv run``, ``timeout``) are skipped.

``history`` is an optional JSON Lines log with one object per finished attempt:
``item`` and ``outcome`` are required; ``started_at`` and ``finished_at`` are
optional; every other key is shown in the inspector.

Paths may contain ``{git_dir}``, resolved from the Git checkout at ``root``. With
``host`` set, the probe is sent to ``python3 -`` over SSH on stdin; nothing is
installed or written on the host.
"""

from __future__ import annotations

import asyncio
import glob
import inspect
import json
import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from ..model import DEFAULT_VIEWS, SCHEMA
from ..process import check_host, over_ssh, run_json
from . import Adapter

OUTCOMES = {
    "success": "success",
    "merged": "success",
    "failure": "failure",
    "failed": "failure",
    "refused": "failure",
    "exception": "failure",
    "error": "failure",
    "cancelled": "cancelled",
}
HISTORY_KEYS = {"item", "outcome", "started_at", "finished_at"}


# Shipped to the queue host over stdin, so it must stay self-contained.
def flock_probe(options: dict, proc_root: str = "/proc") -> dict:
    git_dir = ""
    if options.get("root"):
        git_dir = subprocess.run(
            [
                "git",
                "-C",
                os.path.expanduser(options["root"]),
                "rev-parse",
                "--absolute-git-dir",
            ],
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        ).stdout.strip()

    def resolve(template):
        if "{git_dir}" in template and not git_dir:
            raise ValueError("{git_dir} needs the root option")
        return os.path.expanduser(template.replace("{git_dir}", git_dir))

    proc = Path(proc_root)
    locks = {}
    for line in (proc / "locks").read_text().splitlines():
        fields = line.split()
        if len(fields) < 6 or fields[1] != "FLOCK":
            continue  # Includes blocked lock requests (the -> rows).
        device = fields[5].split(":")
        if len(device) == 3:
            locks[(int(device[0], 16), int(device[1], 16), int(device[2]))] = int(fields[4])

    def holder(path):
        try:
            stat = os.stat(path)
        except FileNotFoundError:
            return None
        return locks.get((os.major(stat.st_dev), os.minor(stat.st_dev), stat.st_ino))

    def command(pid):
        try:
            raw = (proc / str(pid) / "cmdline").read_bytes()
        except (FileNotFoundError, ProcessLookupError, PermissionError):
            return []
        return raw.decode("utf-8", errors="replace").rstrip("\0").split("\0")

    name = options["process"]
    interpreters = tuple(options.get("interpreters", ("python", "pypy")))
    owner_flag = options.get("owner_flag")

    def match(argv):
        """Return (item, owner) when argv is the merger itself, not a wrapper."""
        if not argv:
            return None
        if Path(argv[0]).name == name:
            rest = argv[1:]
        elif Path(argv[0]).name.startswith(interpreters):
            for index, arg in enumerate(argv[1:], 1):
                if Path(arg).name == name:
                    rest = argv[index + 1 :]
                    break
            else:
                return None
        else:
            return None
        item = rest[0] if rest and not rest[0].startswith("-") else "unknown"
        owner = None
        if owner_flag and owner_flag in rest:
            at = rest.index(owner_flag) + 1
            owner = rest[at] if at < len(rest) else None
        return item, owner

    active = holder(resolve(options["lock"]))
    rows, seen = [], set()
    for ticket in sorted(glob.glob(resolve(options["tickets"]))):
        pid = holder(ticket)
        if pid is None:
            continue  # Abandoned tickets are not queue entries. Never delete them.
        try:
            modified = os.stat(ticket).st_mtime
        except FileNotFoundError:
            continue  # A merger finished between observations.
        item, owner = match(command(pid)) or ("unknown", None)
        seen.add(pid)
        rows.append(
            {
                "id": f"ticket:{Path(ticket).name}:{pid}",
                "item": item,
                "owner": owner,
                "state": "integrating" if pid == active else "queued",
                "position": len(rows) + 1,
                "pid": pid,
                "since": datetime.fromtimestamp(modified, UTC).isoformat(),
                "ticket": Path(ticket).name,
            }
        )
    # A holder without a ticket stays visible without an invented position.
    if active is not None and active not in seen:
        item, owner = match(command(active)) or ("unknown", None)
        rows.insert(
            0,
            {
                "id": f"holder:{active}",
                "item": item,
                "owner": owner,
                "state": "integrating",
                "position": None,
                "pid": active,
            },
        )
        seen.add(active)
    inaccessible = 0
    for entry in proc.iterdir():
        if not entry.name.isdigit() or int(entry.name) in seen:
            continue
        try:
            if entry.stat().st_uid != os.getuid():
                continue
            raw = (entry / "cmdline").read_bytes()
        except (FileNotFoundError, ProcessLookupError):
            continue
        except PermissionError:
            inaccessible += 1
            continue
        info = match(raw.decode("utf-8", errors="replace").rstrip("\0").split("\0"))
        if info is not None:
            rows.append(
                {
                    "id": f"process:{entry.name}",
                    "item": info[0],
                    "owner": info[1],
                    "state": "preparing",
                    "position": None,
                    "pid": int(entry.name),
                }
            )
    limit = int(options.get("history_limit", 30))
    recent, malformed = [], 0
    if options.get("history"):
        try:
            with open(resolve(options["history"]), "rb") as stream:
                size = stream.seek(0, 2)
                offset = max(0, size - 262144)
                stream.seek(offset)
                if offset:
                    stream.readline()  # Discard the potentially partial first record.
                lines = stream.read().splitlines(keepends=True)
            for line in lines:
                if not line.endswith(b"\n"):
                    continue  # A concurrent append is not a completed record.
                try:
                    record = json.loads(line)
                    if not isinstance(record, dict) or "item" not in record:
                        raise ValueError("invalid history record")
                    recent.append(record)
                except (ValueError, UnicodeDecodeError):
                    malformed += 1
        except FileNotFoundError:
            pass
    warnings = []
    if malformed:
        warnings.append(f"Skipped {malformed} malformed history records")
    if inaccessible:
        warnings.append(f"Cannot inspect {inaccessible} same-user processes")
    return {
        "observed_at": datetime.now(UTC).isoformat(),
        "queue": rows,
        "history": list(reversed(recent[-limit:])),
        "warnings": warnings,
        "history_limit": limit,
    }


PROBE_PRELUDE = (
    "import glob, json, os, subprocess\n"
    "from datetime import UTC, datetime\n"
    "from pathlib import Path\n"
)


class FlockQueue(Adapter):
    name = "flock-queue"
    label = "Integration"
    interval = 5.0

    def __init__(
        self,
        *,
        lock: str,
        tickets: str,
        process: str,
        root: str | None = None,
        host: str | None = None,
        interpreters: list[str] | None = None,
        owner_flag: str | None = None,
        history: str | None = None,
        history_limit: int = 30,
        history_columns: list[dict] | None = None,
    ):
        self.host = check_host(host)
        self.options = {
            "root": root,
            "lock": lock,
            "tickets": tickets,
            "process": process,
            "owner_flag": owner_flag,
            "history": history,
            "history_limit": int(history_limit),
        }
        if interpreters is not None:
            self.options["interpreters"] = list(interpreters)
        self.history_columns = list(history_columns or [])

    async def probe(self) -> dict:
        if self.host is None:
            return await asyncio.to_thread(flock_probe, self.options)
        source = PROBE_PRELUDE + inspect.getsource(flock_probe)
        source += f"\nprint(json.dumps(flock_probe(json.loads({json.dumps(self.options)!r}))))\n"
        return await run_json(over_ssh(self.host, ["python3", "-"]), stdin=source)

    async def snapshot(self) -> dict:
        raw = await self.probe()
        records = []
        for row in raw["queue"]:
            records.append(
                {
                    "id": row["id"],
                    "kind": "queue_entry",
                    "state": "queued" if row["state"] == "queued" else "running",
                    "raw_state": row["state"],
                    "title": row["item"],
                    "owner": row.get("owner"),
                    "position": row.get("position"),
                    "created_at": row.get("since"),
                    "fields": {key: row[key] for key in ("pid", "ticket") if key in row},
                }
            )
        for index, entry in enumerate(raw["history"]):
            outcome = str(entry.get("outcome", "unknown"))
            records.append(
                {
                    "id": f"attempt:{index}:{entry.get('item')}:{entry.get('finished_at')}",
                    "kind": "attempt",
                    "state": OUTCOMES.get(outcome, "unknown"),
                    "raw_state": outcome,
                    "title": str(entry["item"]),
                    "started_at": timestamp(entry.get("started_at")),
                    "finished_at": timestamp(entry.get("finished_at")),
                    "fields": {
                        key: value for key, value in entry.items() if key not in HISTORY_KEYS
                    },
                }
            )
        attempts = dict(DEFAULT_VIEWS["attempt"])
        attempts["columns"] = attempts["columns"] + self.history_columns
        return {
            "schema": SCHEMA,
            "observed_at": raw["observed_at"],
            "records": records,
            "views": [DEFAULT_VIEWS["queue_entry"], attempts],
            "warnings": raw["warnings"],
            "note": "FIFO positions are observed, not reserved. Preparing mergers hold "
            f"no ticket yet. History is the last {raw['history_limit']} attempts "
            "within a 256 KiB log tail.",
        }


def timestamp(value) -> str | None:
    """History logs are written by other tools; drop unparseable times."""
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    return parsed.isoformat() if parsed.tzinfo else None
