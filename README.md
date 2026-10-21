# queue-watch

Read-only terminal dashboard for CI runs and merge queues. It joins records from every source into one row per change, so a branch's CI runs and its merge-queue entry read together, and draws each merge queue as a chain of chips in queue order. queue-watch never reruns, cancels, merges or takes a lock.

## Install

```sh
uv tool install git+https://github.com/wolfiesch/queue-watch
```

Requires Python 3.11+. The GitHub adapters use an authenticated [GitHub CLI](https://cli.github.com/) (`gh auth login`).

## Use

```sh
queue-watch                              # inside a GitHub checkout
queue-watch --repo owner/repo            # Actions plus the merge queue, if any
queue-watch --profile work               # ~/.config/queue-watch/work.toml
queue-watch --config sources.toml        # any combination of sources
queue-watch --repo owner/repo --once     # one JSON snapshot, no TUI
queue-watch --theme nord --ascii         # any Textual theme; ASCII glyphs only
queue-watch --density comfortable        # title, owner and PR on a second line
```

With no flags, queue-watch looks for a `.queue-watch.toml` at the root of the current Git checkout. Without one, it watches the checkout's GitHub repository: Actions runs, plus the merge queue when the default branch has one. `--profile NAME` reads `$XDG_CONFIG_HOME/queue-watch/NAME.toml` (default `~/.config`).

The Changes tab groups changes into Needs attention, Running, Queued and Done; every view a source reports also gets its own tab. Done starts folded while any other group has changes; press enter on a group row, or click it, to fold or unfold it. Select a change or a queue chip to see its CI checks, the jobs of the most notable one, its queue position and ETA, and recent history in the inspector.

Each merge queue gets a strip above the tabs: its entries in merge order, when it should clear, merges in the last hour, and how long recent attempts took. Queue depth over the session appears as a sparkline once two fetches have answered; sources report only the current queue, so the history starts when queue-watch does.

Keyboard: arrows to move, `1` to `9` for tabs, `/` to search (esc to clear), `o` to open the selection in a browser, `c` to copy its SHA, `d` to switch row density, `r` to refresh, `p` to pause, `q` to quit. The mouse works too: click tabs, rows, chips and the inspector buttons, and scroll with the wheel. When a source fails, its last good data stays on screen and its status pill turns stale.

### Repository configs need trust

A config can run commands and open SSH connections, so a `.queue-watch.toml` that arrives with a clone does nothing until you review it and run:

```sh
queue-watch trust
```

Trust covers the file's exact bytes (SHA-256, stored in `$XDG_STATE_HOME/queue-watch/trusted.json`). After any edit, queue-watch refuses the file and names the command to run again. Files passed with `--config` or `--profile` are yours and load without this step.

## Sources

A config file lists sources as `[[source]]` tables. `id` and `adapter` are required; `label` and `interval` (seconds) are optional; every other key is an adapter option.

```toml
[[source]]
id = "ci"
adapter = "github-actions"
repo = "owner/repo"

[[source]]
id = "merge-queue"
adapter = "github-merge-queue"
repo = "owner/repo"
branch = "main"                          # optional; default branch otherwise

[[source]]
id = "deploys"
adapter = "command"
command = ["./bin/deploy-status", "--json"]
timeout = 20                             # optional seconds, default 25

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
| `github-merge-queue` | GitHub's native merge queue: position, state, author and GitHub's merge estimate per pull request | `repo`, `branch` |
| `command` | Anything else: a program that prints one snapshot | `command`, `host`, `cwd`, `timeout` |
| `flock-queue` | A Linux FIFO of flock-held ticket files guarding one merge lock, plus an optional JSON Lines attempt log | `lock`, `tickets`, `process`, `root`, `host`, `interpreters`, `owner_flag`, `history`, `history_limit`, `history_columns` |

Merge queue states map as `QUEUED` and `MERGEABLE` to queued, `AWAITING_CHECKS` and `LOCKED` to running, and `UNMERGEABLE` to blocked. A branch without a queue shows a warning instead of an error.

`flock-queue` reads lock holders from `/proc/locks` rather than acquiring them. With `host` set, the probe goes to `python3 -` over SSH on stdin and nothing is installed on the host.

### Command sources

Use the `command` adapter for GitLab, Buildkite, Jenkins, Bors or an in-house queue. `command` is an argument list run without a shell. Locally it runs in `cwd`, which defaults to the config file's directory; relative paths resolve against that directory too. With `host` set, it runs over SSH (`BatchMode`, every argument quoted for the remote shell), in `cwd` on the host when given.

The program must exit zero and print one snapshot (below) on stdout within `timeout` seconds. Anything else, including a snapshot that breaks the contract, shows as a source error while the last good data stays on screen. [`examples/`](examples/) has a working script and config.

## Snapshot contract

Every adapter emits the same versioned snapshot (`queue-watch.snapshot/1`): a list of records with `id`, `kind`, `state` and `title`, plus optional `ref`, `sha`, `owner`, `position`, timestamps, `url`, `fields` and `children`. `state` is one of `queued`, `running`, `blocked`, `success`, `failure`, `cancelled`, `neutral` or `unknown`; the adapter's own term is kept in `raw_state`. Records that share a `ref` or `sha` describe the same change across sources. [`model.py`](src/queue_watch/model.py) defines and validates the contract, and `--once` prints it.

## License

MIT
