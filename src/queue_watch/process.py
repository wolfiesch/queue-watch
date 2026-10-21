"""Subprocess and text helpers shared by adapters and views."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import shlex
import signal
from typing import Any


def check_host(host: str | None) -> str | None:
    """An SSH destination, never something ssh would parse as an option."""
    if host is not None and (not isinstance(host, str) or not host or host.startswith("-")):
        raise ValueError("host must be an SSH destination, not an option")
    return host


def over_ssh(host: str, argv: list[str], cwd: str | None = None) -> list[str]:
    """Run ``argv`` on ``host`` with every argument quoted for the remote shell."""
    command = shlex.join(argv)
    if cwd:
        # Keep a leading ~/ unquoted so the remote shell expands it.
        target = "~/" + shlex.quote(cwd[2:]) if cwd.startswith("~/") else shlex.quote(cwd)
        command = f"cd {target} && {command}"
    return ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", host, "--", command]


async def capture(
    argv: list[str], *, stdin: str | None = None, timeout: float = 25, cwd: str | None = None
) -> tuple[int, bytes, bytes]:
    """Run ``argv`` and return ``(returncode, stdout, stderr)``.

    The command gets its own process group so a timeout also stops children that
    inherited its output pipes; otherwise they would hold the read open.
    """
    process = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd,
        start_new_session=True,
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            process.communicate(stdin.encode() if stdin is not None else None), timeout
        )
    except (TimeoutError, asyncio.CancelledError):
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        # A child that left the group can still hold the pipes; do not wait on it.
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(process.communicate(), 2)
        raise
    return process.returncode, stdout, stderr


def failure(argv: list[str], returncode: int, stderr: bytes) -> RuntimeError:
    message = stderr.decode(errors="replace").strip()[-1200:]
    return RuntimeError(message or f"{argv[0]} exited {returncode}")


async def run_json(
    argv: list[str], *, stdin: str | None = None, timeout: float = 25, cwd: str | None = None
) -> Any:
    """Run ``argv`` and parse stdout as JSON; a nonzero exit raises RuntimeError."""
    returncode, stdout, stderr = await capture(argv, stdin=stdin, timeout=timeout, cwd=cwd)
    if returncode:
        raise failure(argv, returncode, stderr)
    return json.loads(stdout)


def clean(value: Any) -> str:
    """Keep remote text literal and strip terminal control characters."""
    return re.sub(r"[\x00-\x1f\x7f-\x9f]", " ", str(value if value is not None else ""))
