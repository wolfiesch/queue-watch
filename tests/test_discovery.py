"""Source discovery: repository config trust, profiles, GitHub fallback."""

from __future__ import annotations

import asyncio
import subprocess

import pytest

from queue_watch import cli, discovery
from queue_watch.adapters import github_merge_queue
from queue_watch.config import ConfigError

CONFIG = '[[source]]\nid = "ci"\nadapter = "github-actions"\nrepo = "o/r"\n'


@pytest.fixture
def checkout(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    repo = tmp_path / "repo"
    (repo / "sub").mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    return repo


def test_repo_config_needs_trust_and_retrust_after_any_edit(checkout):
    path = checkout / discovery.CONFIG_NAME
    path.write_text(CONFIG)
    with pytest.raises(ConfigError, match="is not trusted yet.*queue-watch trust"):
        asyncio.run(discovery.detect(checkout / "sub"))

    discovery.trust(path)
    (source,) = asyncio.run(discovery.detect(checkout / "sub"))
    assert source.id == "ci"

    path.write_text(CONFIG.replace('"ci"', '"build"'))
    with pytest.raises(ConfigError, match="changed since it was trusted"):
        asyncio.run(discovery.detect(checkout))


def test_a_config_that_cannot_load_is_never_trusted(checkout):
    path = checkout / discovery.CONFIG_NAME
    path.write_text('[[source]]\nid = "a"\nadapter = "jenkins"\n')
    with pytest.raises(ConfigError, match="adapter must be one of"):
        discovery.trust(path)
    assert not (checkout.parent / "state" / "queue-watch" / "trusted.json").exists()


def test_trust_command_trusts_the_checkout_config(checkout, monkeypatch, capsys):
    (checkout / discovery.CONFIG_NAME).write_text(CONFIG)
    monkeypatch.chdir(checkout / "sub")
    assert cli.main(["trust"]) == 0
    assert "ci: github-actions" in capsys.readouterr().out
    assert len(asyncio.run(discovery.detect(checkout))) == 1


@pytest.mark.parametrize("queued", [True, False])
def test_github_fallback_adds_merge_queue_only_when_present(checkout, monkeypatch, queued):
    async def has_queue(repo):
        assert repo == "o/r"
        return queued

    monkeypatch.setattr(discovery, "github_repo", lambda cwd: "o/r")
    monkeypatch.setattr(github_merge_queue, "has_queue", has_queue)
    sources = asyncio.run(discovery.detect(checkout))
    expected = ["github-actions", "github-merge-queue"] if queued else ["github-actions"]
    assert [source.adapter.name for source in sources] == expected


def test_no_checkout_and_no_config_names_every_option(tmp_path, monkeypatch):
    monkeypatch.setattr(discovery, "github_repo", lambda cwd: None)
    with pytest.raises(ConfigError, match="--repo, --config or --profile"):
        asyncio.run(discovery.detect(tmp_path))


def test_profiles_live_under_xdg_config_home(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert discovery.profile_path("work") == tmp_path / "queue-watch" / "work.toml"
    for bad in ("../work", ".hidden", ""):
        with pytest.raises(ConfigError, match="plain name"):
            discovery.profile_path(bad)
