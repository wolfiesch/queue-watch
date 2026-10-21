# Changelog

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
