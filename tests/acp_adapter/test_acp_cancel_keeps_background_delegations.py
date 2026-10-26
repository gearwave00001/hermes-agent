"""ACP ``cancel`` is a TURN stop: it hard-cancels the live prompt and leaves the
session's detached background delegations running (same contract as the TUI
``session.interrupt`` path pinned in
``tests/tui_gateway/test_delegation_session_lifecycle.py``). Note the adapter has
no session-end surface — cancel was historically the only delegation-kill ACP
had; if it ever grows ``close_session``, that path must wire
``interrupt_for_session`` itself.
"""
import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from acp_adapter.server import HermesACPAgent


def _agent_with_fake_state():
    state = SimpleNamespace(
        cancel_event=threading.Event(),
        runtime_lock=threading.RLock(),
        is_running=True,
        current_prompt_text="working",
        interrupted_prompt_text=None,
        agent=MagicMock(),
    )
    manager = MagicMock()
    manager.get_session.return_value = state
    return HermesACPAgent(session_manager=manager), state


def test_acp_cancel_hard_cancels_the_turn_only():
    agent, state = _agent_with_fake_state()

    with patch("acp_adapter.server.request_hard_interrupt") as mock_hard, \
            patch("tools.async_delegation.interrupt_for_session") as mock_deleg:
        asyncio.run(agent.cancel("s1"))

    mock_hard.assert_called_once_with(state.agent)
    mock_deleg.assert_not_called()
    assert state.cancel_event.is_set()
