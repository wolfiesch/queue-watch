"""Record docs/demo.gif from ``queue-watch --demo``.

    uv run python docs/record_demo.py

Needs Google Chrome (set CHROME to its binary elsewhere than macOS) to rasterise
the SVG frames, and ffmpeg to assemble the GIF.
"""

from __future__ import annotations

import asyncio
import os
import re
import subprocess
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from queue_watch import demo, widgets
from queue_watch.app import QueueWatch

SIZE = (112, 32)
SECONDS = 32
FPS = 2
WIDTH = 1000  # GIF width in pixels
# Keys pressed at a given second: walk the change list, visit the queue tab, return.
KEYS = {6: "down", 8: "down", 10: "down", 12: "down", 17: "3", 25: "1", 27: "up"}
CHROME = os.environ.get("CHROME", "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
OUT = Path(__file__).with_name("demo.gif")


async def capture(frames: Path) -> list[float]:
    os.environ.pop("NO_COLOR", None)  # the GIF shows colour whatever the shell asks for
    clock = demo.Clock()
    widgets.now = clock.now
    app = QueueWatch(demo.sources(clock))
    stamps = []
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause(2)
        start = time.monotonic()
        pressed = set()
        for frame in range(SECONDS * FPS):
            second = frame // FPS
            if second in KEYS and second not in pressed:
                pressed.add(second)
                await pilot.press(KEYS[second])
            await pilot.pause(max(0, start + frame / FPS - time.monotonic()))
            svg = app.export_screenshot(title="queue-watch --demo")
            (frames / f"{frame:03}.svg").write_text(svg)
            stamps.append(time.monotonic() - start)
    return stamps


def rasterise(svg: Path) -> None:
    width, height = re.search(r'viewBox="0 0 ([\d.]+) ([\d.]+)"', svg.read_text()).groups()
    with tempfile.TemporaryDirectory() as profile:
        subprocess.run(
            [CHROME, "--headless=new", f"--user-data-dir={profile}", "--hide-scrollbars",
             "--force-device-scale-factor=1", f"--window-size={round(float(width))},{round(float(height))}",
             f"--screenshot={svg.with_suffix('.png')}", svg.as_uri()],
            check=True, capture_output=True,
        )  # fmt: skip


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        frames = Path(tmp)
        stamps = asyncio.run(capture(frames))
        with ThreadPoolExecutor(8) as pool:
            list(pool.map(rasterise, sorted(frames.glob("*.svg"))))
        # Each frame lasts until the next one was captured.
        lines = []
        for index, stamp in enumerate(stamps):
            end = stamps[index + 1] if index + 1 < len(stamps) else stamp + 1 / FPS
            lines += [f"file '{frames / f'{index:03}.png'}'", f"duration {end - stamp:.3f}"]
        (frames / "frames.txt").write_text("\n".join(lines) + "\n")
        palette = "split[a][b];[a]palettegen=stats_mode=diff[p];[b][p]paletteuse=dither=none"
        subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i",
             frames / "frames.txt", "-vf", f"scale={WIDTH}:-1:flags=lanczos,{palette}",
             "-loop", "0", OUT],
            check=True,
        )  # fmt: skip
    print(f"{OUT} {OUT.stat().st_size // 1024} KiB")


if __name__ == "__main__":
    main()
