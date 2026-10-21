"""The command adapter: config-relative execution, contract enforcement, SSH quoting."""

from __future__ import annotations

import asyncio
import json
import sys

import pytest

from queue_watch.config import ConfigError, load
from queue_watch.model import SCHEMA, SnapshotError
from queue_watch.process import over_ssh

SCRIPT = """
import json, os
print(json.dumps({
    "schema": %r,
    "observed_at": "2026-10-20T00:00:00+00:00",
    "records": [{"id": "1", "kind": "run", "state": %r, "title": os.path.basename(os.getcwd())}],
}))
"""


def config(tmp_path, state="running", cwd=None):
    folder = tmp_path / "conf dir"
    (folder / "bin").mkdir(parents=True)
    (folder / "bin" / "status.py").write_text(SCRIPT % (SCHEMA, state))
    path = folder / "sources.toml"
    lines = [
        "[[source]]",
        'id = "custom"',
        'adapter = "command"',
        f"command = {json.dumps([sys.executable, 'bin/status.py'])}",
    ]
    if cwd:
        lines.append(f"cwd = {json.dumps(cwd)}")
    path.write_text("\n".join(lines) + "\n")
    return path


def test_command_runs_relative_to_the_config_file(tmp_path, monkeypatch):
    path = config(tmp_path)
    monkeypatch.chdir(tmp_path)
    (source,) = load(path)
    snapshot = asyncio.run(source.snapshot())
    assert snapshot["records"][0]["title"] == "conf dir"


def test_relative_cwd_is_resolved_against_the_config_file(tmp_path, monkeypatch):
    path = config(tmp_path, cwd="bin")
    (path.parent / "bin" / "bin").mkdir()
    (path.parent / "bin" / "bin" / "status.py").write_text(SCRIPT % (SCHEMA, "queued"))
    monkeypatch.chdir(tmp_path)
    (source,) = load(path)
    assert asyncio.run(source.snapshot())["records"][0]["title"] == "bin"


def test_output_that_breaks_the_contract_is_an_error(tmp_path):
    (source,) = load(config(tmp_path, state="in_progress"))
    with pytest.raises(SnapshotError, match="state must be one of"):
        asyncio.run(source.snapshot())


def test_command_must_be_an_argument_list(tmp_path):
    path = tmp_path / "sources.toml"
    path.write_text('[[source]]\nid = "a"\nadapter = "command"\ncommand = "status --json"\n')
    with pytest.raises(ConfigError, match="list of arguments"):
        load(path)


def test_config_dir_cannot_be_set_from_configuration(tmp_path):
    path = tmp_path / "sources.toml"
    path.write_text(
        '[[source]]\nid = "a"\nadapter = "command"\ncommand = ["x"]\nconfig_dir = "/"\n'
    )
    with pytest.raises(ConfigError, match="does not accept config_dir"):
        load(path)


def test_ssh_arguments_reach_the_remote_shell_quoted():
    argv = over_ssh("box", ["status", "a; touch pwned", "$(id)"], "~/my dir")
    assert argv[-3:] == ["box", "--", "cd ~/'my dir' && status 'a; touch pwned' '$(id)'"]


@pytest.mark.parametrize("host", ["-oProxyCommand=sh", ""])
def test_host_cannot_be_an_ssh_option(tmp_path, host):
    path = tmp_path / "sources.toml"
    path.write_text(
        f'[[source]]\nid = "a"\nadapter = "command"\ncommand = ["x"]\nhost = "{host}"\n'
    )
    with pytest.raises(ConfigError, match="SSH destination"):
        load(path)
