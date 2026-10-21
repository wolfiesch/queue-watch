#!/usr/bin/env python3
"""Example source for the ``command`` adapter: a queue kept as a JSON file.

queue-watch runs this with no shell and reads one snapshot v1 object from
stdout. Adapt the ``load`` function to whatever your system exposes: an HTTP
API, a database query, a directory of ticket files.

    [[source]]
    id = "deploys"
    adapter = "command"
    command = ["python3", "examples/queue-status.py", "examples/queue.json"]
"""

import json
import sys
from datetime import UTC, datetime

STATES = {"waiting": "queued", "deploying": "running", "held": "blocked"}


def load(path: str) -> list[dict]:
    with open(path) as handle:
        return json.load(handle)


def main() -> None:
    entries = load(sys.argv[1])
    records = [
        {
            "id": str(entry["id"]),
            "kind": "queue_entry",
            "state": STATES.get(entry["status"], "unknown"),
            "raw_state": entry["status"],
            "title": entry["title"],
            "owner": entry.get("owner"),
            "position": position,
            "created_at": entry.get("since"),
            "fields": {"target": entry.get("target")},
        }
        for position, entry in enumerate(entries, start=1)
    ]
    snapshot = {
        "schema": "queue-watch.snapshot/1",
        "observed_at": datetime.now(UTC).isoformat(),
        "records": records,
        "views": [
            {
                "kind": "queue_entry",
                "label": "Deploys",
                "columns": [
                    {"key": "position", "label": "#", "width": 4},
                    {"key": "state", "label": "State", "width": 12, "format": "state"},
                    {"key": "title", "label": "Change", "width": 36},
                    {"key": "fields.target", "label": "Target", "width": 12},
                    {"key": "created_at", "label": "Waiting", "width": 9, "format": "age"},
                ],
            }
        ],
    }
    json.dump(snapshot, sys.stdout)


if __name__ == "__main__":
    main()
