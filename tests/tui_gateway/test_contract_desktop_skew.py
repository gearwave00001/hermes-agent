"""Regression: a Desktop built at/after c57331677d must pass param admission on this backend.

The desktop ships ahead of the packaged backend: its branch/fork path always sends
``idempotency_key`` (#65410) and its create path sends ``cwd_explicit`` (#52589) and
``service_tier``. ``validate_params`` rejects unknown keys with 4000 carrying the
"out of sync (different versions)" text, so a contract that omits fields the handler
already honours breaks fork/branch with a version-skew error even though the backend
supports the operation. These are admission contracts, not snapshots: they state which
params ``session.create`` / branch methods must accept.
"""

from __future__ import annotations

import pytest

from tui_gateway.contracts import registry as contracts


@pytest.mark.parametrize("extra", [
    {"idempotency_key": "branch-abc"},
    {"cwd_explicit": True},
    {"service_tier": "ultrafast"},
    {"cwd": "/tmp", "cwd_explicit": True, "fast": True, "service_tier": "ultrafast"},
])
def test_session_create_accepts_desktop_shipped_params(extra):
    contract = contracts.METHODS["session.create"]
    params = {"cols": 96, "source": "desktop", "parent_session_id": "p1",
              "messages": [{"role": "user", "content": "hi"}], **extra}
    _, problem = contracts.validate_params(contract, params)
    assert problem is None, f"session.create rejected a shipped desktop field: {problem}"


def test_branch_methods_accept_idempotency_key():
    for name in ("session.branch", "session.branch_whole", "session.branch_stored"):
        contract = contracts.METHODS[name]
        params = ({"parent_session_id": "p1", "idempotency_key": "k1"}
                  if name == "session.branch_stored"
                  else {"session_id": "s1", "idempotency_key": "k1"})
        _, problem = contracts.validate_params(contract, params)
        assert problem is None, f"{name} rejected idempotency_key: {problem}"
