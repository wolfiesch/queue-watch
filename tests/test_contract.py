"""Snapshot v1 validation and source configuration errors."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from queue_watch.config import ConfigError, load
from queue_watch.model import SCHEMA, SnapshotError, validate_snapshot, views_for

FIXTURE = Path(__file__).resolve().parents[1] / "design" / "fixtures" / "demo.json"


def snapshot(*records, **extra):
    return {
        "schema": SCHEMA,
        "observed_at": "2026-10-20T00:00:00+00:00",
        "records": list(records),
        **extra,
    }


def record(**overrides):
    return {"id": "1", "kind": "run", "state": "running", "title": "CI", **overrides}


def test_demo_fixture_satisfies_the_contract():
    for source in json.loads(FIXTURE.read_text())["sources"].values():
        validate_snapshot(source["snapshot"])


@pytest.mark.parametrize(
    ("bad", "message"),
    [
        (snapshot(record(state="in_progress")), "state must be one of"),
        (snapshot(record(created_at="2026-10-20T00:00:00")), "needs a UTC offset"),
        (snapshot(record(), record()), "duplicated"),
        (snapshot(record(position=True)), "position must be an integer"),
        (snapshot(record(children=[{"id": "j"}])), r"children\[0\]"),
        ({**snapshot(), "schema": "queue-watch.snapshot/2"}, "schema must be"),
        (
            snapshot(views=[{"kind": "run", "columns": [{"key": "x", "format": "bold"}]}]),
            "format must be one of",
        ),
    ],
)
def test_invalid_snapshots_are_rejected(bad, message):
    with pytest.raises(SnapshotError, match=message):
        validate_snapshot(bad)


def test_undeclared_kinds_get_default_views_after_declared_ones():
    declared = {"kind": "attempt", "label": "Mine", "columns": [{"key": "title"}]}
    views = views_for(
        snapshot(record(kind="queue_entry"), record(id="2", kind="custom"), views=[declared])
    )
    assert [(v["kind"], v["label"]) for v in views] == [
        ("attempt", "Mine"),
        ("queue_entry", "Queue"),
        ("custom", "Custom"),
    ]


@pytest.mark.parametrize(
    ("toml", "message"),
    [
        ('[[source]]\nid = "a"\nadapter = "jenkins"\n', "adapter must be one of"),
        (
            '[[source]]\nid = "a"\nadapter = "github-actions"\nrepo = "o/r"\ntoken = "x"\n',
            "does not accept token",
        ),
        ('[[source]]\nid = "a"\nadapter = "flock-queue"\nlock = "l"\n', "needs tickets, process"),
        (
            '[[source]]\nid = "a"\nadapter = "github-actions"\nrepo = "o/r"\n' * 2,
            "duplicate source id a",
        ),
        (
            '[[source]]\nid = "a"\nadapter = "github-actions"\nrepo = "o/r"\ninterval = 0\n',
            "interval",
        ),
        ("title = 1\n", "at least one"),
    ],
)
def test_config_errors_name_the_problem(tmp_path, toml, message):
    path = tmp_path / "sources.toml"
    path.write_text(toml)
    with pytest.raises(ConfigError, match=message):
        load(path)
