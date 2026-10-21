# Changelog

## Unreleased

- The `github-actions` adapter sends each list's last ETag back with `If-None-Match`. GitHub does not count a `304 Not Modified` against the API rate limit, so run lists and job lists that have not changed since the last refresh no longer use up the account's hourly budget. Against a busy repository, 5 of the 6 run-list requests per refresh came back unchanged.

## 0.3.0 - 2026-10-21

- Profiles for groups of repositories: `queue-watch profile add NAME --repo owner/repo [--repo ...]` creates or extends `~/.config/queue-watch/NAME.toml`, and `queue-watch profile list` shows every profile and its sources.
- Outside a GitHub checkout, `queue-watch` opens the profile named `default`.
- Fix: a dashboard watching several repositories no longer folds the same branch name (such as `main`) from different repositories into one row. Records carry an optional `repo`; rows and the inspector name it when more than one repository is shown.

## 0.2.0 - 2026-10-21

- `--demo` plays a simulated repository whose CI runs and merge queue move on their own: merges land, checks fail and get fixed, and ages tick. No network or GitHub login needed.
- New default theme, `queue-watch`: a solid black background with a coloured box around each queue, the tabs and the inspector. `--theme` still takes any Textual theme.
- Queue names move into their strip's border title.

## 0.1.0 - 2026-10-20

First release.

- One row per change across sources: a branch's CI runs and its merge queue entry read together, grouped into Needs attention, Running, Queued and Done.
- Merge queue strips: entries in merge order as chips, time to clear, merges in the last hour, attempt durations and a depth sparkline.
- Inspector with CI checks, job timings, queue position and ETA, and history.
- Adapters: `github-actions`, `github-merge-queue`, `command` (any program that prints a snapshot, locally or over SSH) and `flock-queue`.
- Zero-config start inside a GitHub checkout; `.queue-watch.toml` files load only after `queue-watch trust`.
- `--once` prints JSON snapshots; `--ascii`, `--density` and `--theme` adjust the display.
