"""Regression coverage for CLI async-delegation completion ownership."""

import queue

from cli import HermesCLI


def test_cli_completion_drain_uses_visible_session_identity(monkeypatch):
    """A CLI window must not claim another window's restored completion.

    Post-merge the dedicated ``_async_delegation_watcher`` / ``skip_async_delegation``
    flag were dropped; the idle drain now routes async-delegation events through the
    ``owns_event`` callback (``_owns_process_notification``). So the contract is:
    an OWNED event is claimed+completed+enqueued, a FOREIGN event is left for its
    owner (not claimed, nothing enqueued).
    """
    cli = HermesCLI.__new__(HermesCLI)
    cli.session_id = "visible-session"
    cli._session_db = None
    cli._pending_input = queue.Queue()

    owned = {
        "type": "async_delegation",
        "delegation_id": "deleg_visible",
        "session_key": "visible-session",
    }
    foreign = {
        "type": "async_delegation",
        "delegation_id": "deleg_foreign",
        "session_key": "foreign-session",
    }
    calls = []

    class FakeRegistry:
        def drain_notifications(
            self, *, session_key="", owns_event=None, skip_poll_observed=True
        ):
            # Mirror the real drain: only events the callback approves are returned;
            # non-owned ones are requeued for their owner.
            out = []
            for event in (owned, foreign):
                calls.append((session_key, owns_event(event)))
                if owns_event(event):
                    out.append((event, "completion payload"))
            return out

    claimed = []
    completed = []

    monkeypatch.setattr(
        "tools.process_registry.process_registry",
        FakeRegistry(),
    )
    monkeypatch.setattr(
        "tools.async_delegation.claim_event_delivery",
        lambda evt, consumer: claimed.append((evt, consumer)) or "claim-token",
    )
    monkeypatch.setattr(
        "tools.async_delegation.complete_event_delivery",
        lambda evt, token: completed.append((evt, token)),
    )

    cli._drain_process_notifications("cli-idle")

    # The visible session identity + ownership callback are consulted for every event.
    assert all(c[0] == "visible-session" for c in calls)
    # Only the owned event is claimed/completed/delivered; the foreign one is left alone.
    assert [e["delegation_id"] for e, _ in claimed] == ["deleg_visible"]
    assert [e["delegation_id"] for e, _ in completed] == ["deleg_visible"]
    assert not cli._pending_input.empty()


def test_cli_completion_ownership_rejects_foreign_session():
    cli = HermesCLI.__new__(HermesCLI)
    cli.session_id = "visible-session"
    cli._session_db = None

    assert not cli._owns_process_notification(
        {"type": "async_delegation", "session_key": "foreign-session"}
    )


def test_cli_completion_ownership_accepts_compression_lineage():
    cli = HermesCLI.__new__(HermesCLI)
    cli.session_id = "visible-session"

    class FakeSessionDB:
        def resolve_resume_session_id(self, session_id):
            assert session_id == "pre-compression-session"
            return "visible-session"

    cli._session_db = FakeSessionDB()

    assert cli._owns_process_notification(
        {
            "type": "async_delegation",
            "session_key": "pre-compression-session",
        }
    )
