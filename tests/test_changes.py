"""Correlating records into changes, and the state and group each change shows."""

from __future__ import annotations

from queue_watch.changes import correlate, grouped


def record(id: str, kind: str, state: str, **extra) -> dict:
    return {"id": id, "kind": kind, "state": state, "title": extra.pop("title", id), **extra}


def snap(*records: dict) -> dict:
    return {"records": list(records)}


def test_sha_joins_a_record_without_a_ref_and_other_kinds_are_ignored() -> None:
    changes = correlate(
        {
            "ci": snap(
                record(
                    "r1",
                    "run",
                    "success",
                    ref="main",
                    sha="abc",
                    created_at="2026-10-20T10:00:00+00:00",
                ),
                record("s1", "service", "running", ref="main"),
            ),
            "mq": snap(record("a1", "attempt", "failure", sha="abc")),
        }
    )
    assert len(changes) == 1
    change = changes[0]
    assert (change.key, change.ref, change.sha) == ("main", "main", "abc")
    assert [hit.record["id"] for hit in change.attempts] == ["a1"]
    # Records with neither ref nor sha never form a change.
    assert correlate({"x": snap(record("n", "run", "failure"))}) == []


def test_ci_state_uses_the_latest_run_of_each_workflow() -> None:
    old = record(
        "r1", "run", "failure", title="test", ref="b", created_at="2026-10-20T09:00:00+00:00"
    )
    new = record(
        "r2", "run", "success", title="test", ref="b", created_at="2026-10-20T10:00:00+00:00"
    )
    lint = record(
        "r3", "run", "running", title="lint", ref="b", created_at="2026-10-20T08:00:00+00:00"
    )
    (change,) = correlate({"ci": snap(old, new, lint)})
    assert [hit.record["id"] for hit in change.checks] == ["r2", "r3"]
    assert change.ci_state == "running" and change.group == "running"


def test_queue_state_outranks_green_ci_and_finished_changes_fall_to_done() -> None:
    changes = correlate(
        {
            "ci": snap(
                record("r1", "run", "success", ref="q"),
                record("r2", "run", "cancelled", ref="gone"),
            ),
            "mq": snap(
                record("e1", "queue_entry", "blocked", ref="q", position=2),
                record("e2", "queue_entry", "queued", ref="first", position=1),
                record("a1", "attempt", "success", ref="merged"),
            ),
        }
    )
    by_key = {change.key: change for change in changes}
    assert by_key["q"].state == "blocked" and by_key["q"].group == "attention"
    assert by_key["merged"].state == "success" and by_key["merged"].group == "done"
    # A cancelled run is final, not something to act on.
    assert by_key["gone"].group == "done"
    # Queue order first: position 1 before position 2, then everything else.
    assert [change.key for change in changes][:2] == ["first", "q"]
    groups = {key: sorted(c.key for c in members) for key, _, _, members in grouped(changes)}
    assert groups == {
        "attention": ["q"],
        "running": [],
        "queued": ["first"],
        "done": ["gone", "merged"],
    }


def test_same_branch_in_different_repositories_stays_apart() -> None:
    api = snap(
        record("r1", "run", "success", ref="main", sha="a1", repo="acme/api"),
        record("r2", "run", "failure", ref="feat/x", sha="a2", repo="acme/api"),
    )
    web = snap(record("r3", "run", "failure", ref="main", sha="b1", repo="acme/web"))
    # A source without repositories (a command adapter) cannot be placed in either.
    local = snap(record("q1", "queue_entry", "queued", ref="main", position=1))
    changes = correlate({"api": api, "web": web, "local": local})
    by_key = {change.key: change for change in changes}
    assert set(by_key) == {"acme/api:main", "acme/api:feat/x", "acme/web:main", "main"}
    assert by_key["acme/web:main"].state == "failure"
    assert by_key["acme/api:main"].state == "success"
    assert (by_key["acme/web:main"].repo, by_key["main"].repo) == ("acme/web", None)

    # Repository names are case-insensitive; an empty one is no repository.
    mixed = snap(
        record("q2", "queue_entry", "queued", ref="main", repo="Acme/Web", position=1),
        record("q3", "queue_entry", "queued", ref="main", repo="", position=2),
    )
    changes = correlate({"api": api, "web": web, "mixed": mixed})
    assert sorted((c.key, len(c.runs), len(c.entries)) for c in changes) == [
        ("acme/api:feat/x", 1, 0),
        ("acme/api:main", 1, 0),
        ("acme/web:main", 1, 1),
        ("main", 0, 1),
    ]

    # With one repository, records without one still join it, and keys stay plain.
    changes = correlate({"api": snap(api["records"][0]), "local": local})
    assert [(change.key, change.repo, len(change.entries)) for change in changes] == [
        ("main", None, 1)
    ]
