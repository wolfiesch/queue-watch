# Design

`mockups/mockup.py` renders two candidate layouts from `fixtures/demo.json` without network access. `mockups/screens/` holds captures at 100x30 and 160x45 in two themes.

```sh
uv run python design/mockups/mockup.py --variant dense                  # interactive
uv run python design/mockups/mockup.py --variant calm --ascii           # no Unicode glyphs
uv run python design/mockups/mockup.py --variant dense --theme tokyo-night \
    --screenshot out.svg --size 160x45
```

Unset `NO_COLOR` before capturing; with it set, Textual renders in greys.

## Decision: dense layout with calm's queue chips

| Element | Adopted from |
|---|---|
| Status bar with one live/stale pill per source | both (shared) |
| Tight table rows, one row per change | dense |
| Inspector as a right-hand column | dense |
| Queue strip: FIFO chain of chips, position 1 highlighted, ETA from `fields.estimated_seconds` | both (shared) |
| Chip style: bordered, multi-line, border title `1 · next` on the holder, heavy primary border on the holder | calm |
| Glyph plus colour state vocabulary, `--ascii` fallback | both (shared) |

Dense keeps the most changes on screen, which matters most when a queue backs up. Calm's bordered chips make queue order and the current holder readable at a glance, where dense's one-line chips blur into the status bar.

Not adopted from calm: one card per change and the bordered inspector drawer.

The real app (`src/queue_watch/app.py`) does not yet follow this direction; adopting it is the next stage.
