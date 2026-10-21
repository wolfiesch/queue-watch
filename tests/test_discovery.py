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
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setattr(discovery, "github_repo", lambda cwd: None)
    with pytest.raises(ConfigError, match="--repo, --config or --profile.*profile add default"):
        asyncio.run(discovery.detect(tmp_path))


def test_default_profile_is_used_only_outside_a_github_checkout(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    (tmp_path / "config" / "queue-watch").mkdir(parents=True)
    discovery.profile_path("default").write_text(CONFIG.replace("o/r", "acme/default"))
    monkeypatch.setattr(github_merge_queue, "has_queue", lambda repo: asyncio.sleep(0, False))

    monkeypatch.setattr(discovery, "github_repo", lambda cwd: None)
    assert [s.adapter.repo for s in asyncio.run(discovery.detect(tmp_path))] == ["acme/default"]
    monkeypatch.setattr(discovery, "github_repo", lambda cwd: "acme/here")
    assert [s.adapter.repo for s in asyncio.run(discovery.detect(tmp_path))] == ["acme/here"]


def test_profiles_live_under_xdg_config_home(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert discovery.profile_path("work") == tmp_path / "queue-watch" / "work.toml"
    for bad in ("../work", ".hidden", ""):
        with pytest.raises(ConfigError, match="plain name"):
            discovery.profile_path(bad)


def test_profile_add_appends_new_repositories_and_keeps_hand_edits(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))

    async def has_queue(repo):
        return repo == "acme/api"

    monkeypatch.setattr(github_merge_queue, "has_queue", has_queue)
    assert cli.main(["profile", "add", "work", "--repo", "acme/api", "--repo", "acme/web"]) == 0
    path = discovery.profile_path("work")
    path.write_text(path.read_text() + "# keep me\n")
    assert cli.main(["profile", "add", "work", "--repo", "acme/web", "--repo", "Acme/Tools"]) == 0
    out = capsys.readouterr().out
    assert "acme/web is already in" in out and "Added Acme/Tools" in out

    assert "# keep me" in path.read_text()
    sources = discovery.load(path)
    assert [(s.id, s.label, s.adapter.name, s.adapter.repo) for s in sources] == [
        ("acme-api-ci", "api CI", "github-actions", "acme/api"),
        ("acme-api-merge-queue", "api merge queue", "github-merge-queue", "acme/api"),
        ("acme-web-ci", "web CI", "github-actions", "acme/web"),
        ("acme-tools-ci", "Tools CI", "github-actions", "Acme/Tools"),
    ]

    assert cli.main(["profile", "list"]) == 0
    listing = capsys.readouterr().out.splitlines()
    assert listing[0] == "work" and "  api merge queue  (github-merge-queue acme/api)" in listing


def test_profile_add_rejects_a_bad_repository_without_writing(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    with pytest.raises(SystemExit):
        cli.main(["profile", "add", "work", "--repo", "not-a-repo"])
    assert "owner/repo" in capsys.readouterr().err
    assert not discovery.profile_path("work").exists()


def test_profile_add_keeps_ids_unique_and_symlinks_and_mode_intact(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setattr(github_merge_queue, "has_queue", lambda repo: asyncio.sleep(0, False))
    real = tmp_path / "dotfiles" / "work.toml"
    real.parent.mkdir()
    real.write_text(CONFIG)
    real.chmod(0o640)
    discovery.profile_dir().mkdir(parents=True)
    discovery.profile_path("work").symlink_to(real)

    path, added, skipped = asyncio.run(
        discovery.add_to_profile("work", ["a-b/c", "a/b-c", "O/R", "a/B-C"])
    )
    assert (added, skipped) == (["a-b/c", "a/b-c"], ["O/R", "a/B-C"])
    assert path.is_symlink() and real.stat().st_mode & 0o777 == 0o640
    assert [s.id for s in discovery.load(path)] == ["ci", "a-b-c-ci", "a-b-c-2-ci"]
