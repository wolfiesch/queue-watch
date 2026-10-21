"""Any program that prints one snapshot v1 JSON object on stdout.

This is the extension point for systems without a built-in adapter (GitLab,
Buildkite, Jenkins, Bors, in-house queues). ``command`` is an argument list run
without a shell. Locally it runs in ``cwd``, which defaults to the directory of
the config file. With ``host`` set it runs over SSH, in ``cwd`` on that host
when given. A nonzero exit, a timeout or output that breaks the contract is
shown as a source error, and the last good data stays on screen.
"""

from __future__ import annotations

import os
from pathlib import Path

from ..process import check_host, over_ssh, run_json
from . import Adapter


class Command(Adapter):
    name = "command"
    label = "Command"
    interval = 15.0

    def __init__(
        self,
        *,
        command: list[str],
        host: str | None = None,
        cwd: str | None = None,
        timeout: float = 25,
        config_dir: Path | None = None,
    ):
        if (
            not isinstance(command, list)
            or not command
            or not all(isinstance(arg, str) and arg for arg in command)
        ):
            raise ValueError(
                'command must be a list of arguments, for example ["./status", "--json"]'
            )
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
            raise ValueError("timeout must be a positive number of seconds")
        self.command, self.host, self.timeout = command, check_host(host), float(timeout)
        if self.host is not None:
            self.cwd = cwd
        else:
            base = config_dir or Path.cwd()
            self.cwd = str(base / os.path.expanduser(cwd)) if cwd else str(base)

    async def snapshot(self) -> dict:
        if self.host is not None:
            return await run_json(over_ssh(self.host, self.command, self.cwd), timeout=self.timeout)
        return await run_json(self.command, cwd=self.cwd, timeout=self.timeout)
