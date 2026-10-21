"""Subprocess and text helpers shared by adapters and views."""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any


async def run_json(argv: list[str], *, stdin: str | None = None, timeout: float = 25) -> Any:
    """Run ``argv`` and parse stdout as JSON; a nonzero exit raises RuntimeError."""
    process = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            process.communicate(stdin.encode() if stdin is not None else None), timeout
        )
    except (TimeoutError, asyncio.CancelledError):
        if process.returncode is None:
            process.kill()
        await process.communicate()
        raise
    if process.returncode:
        message = stderr.decode(errors="replace").strip()[-1200:]
        raise RuntimeError(message or f"{argv[0]} exited {process.returncode}")
    return json.loads(stdout)


def clean(value: Any) -> str:
    """Keep remote text literal and strip terminal control characters."""
    return re.sub(r"[\x00-\x1f\x7f-\x9f]", " ", str(value if value is not None else ""))
