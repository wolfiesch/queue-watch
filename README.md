# queue-watch

Read-only terminal dashboard for CI runs and merge queues. Click tabs, rows and buttons, scroll with the wheel, and inspect jobs without leaving the terminal. queue-watch never reruns, cancels, merges or takes a lock.

## Install

```sh
uv tool install git+https://github.com/wolfiesch/queue-watch
```

Requires Python 3.11+. The GitHub Actions adapter uses an authenticated [GitHub CLI](https://cli.github.com/) (`gh auth login`).

## Use

```sh
queue-watch --repo owner/repo            # GitHub Actions for one repository
queue-watch --config sources.toml        # any combination of sources
queue-watch --repo owner/repo --once     # one JSON snapshot, no TUI
```

Keyboard: tab and arrows to move, `/` to search, `r` to refresh, `p` to pause, `o` to open the selected record, `q` to quit. When a source fails, its last good data stays on screen and is labelled stale.

## Sources

A config file lists sources as `[[source]]` tables. `id` and `adapter` are required; `label` and `interval` (seconds) are optional; every other key is an adapter option.

```toml
[[source]]
id = "ci"
adapter = "github-actions"
repo = "owner/repo"

[[source]]
id = "merges"
adapter = "flock-queue"
host = "build-host"                      # optional SSH destination
root = "~/src/project"                   # Git checkout that defines {git_dir}
lock = "{git_dir}/merge.lock"
tickets = "{git_dir}/merge.queue/*.ticket"
process = "merge.py"
owner_flag = "--owner"
history = "{git_dir}/merge-history.jsonl"
history_columns = [{ key = "fields.duration_seconds", label = "Took", format = "seconds" }]
```

| Adapter | Watches | Options |
|---|---|---|
| `github-actions` | Workflow runs, with jobs and steps in the inspector | `repo` |
| `flock-queue` | A Linux FIFO of flock-held ticket files guarding one merge lock, plus an optional JSON Lines attempt log | `lock`, `tickets`, `process`, `root`, `host`, `interpreters`, `owner_flag`, `history`, `history_limit`, `history_columns` |

`flock-queue` reads lock holders from `/proc/locks` rather than acquiring them. With `host` set, the probe goes to `python3 -` over SSH on stdin and nothing is installed on the host.

## Snapshot contract

Every adapter emits the same versioned snapshot (`queue-watch.snapshot/1`): a list of records with `id`, `kind`, `state` and `title`, plus optional `ref`, `sha`, `owner`, `position`, timestamps, `url`, `fields` and `children`. `state` is one of `queued`, `running`, `blocked`, `success`, `failure`, `cancelled`, `neutral` or `unknown`; the adapter's own term is kept in `raw_state`. Records that share a `ref` or `sha` describe the same change across sources. [`model.py`](src/queue_watch/model.py) defines and validates the contract, and `--once` prints it.

## License

MIT
