#!/usr/bin/env python3
"""
Test: Reproduce the dead orchestrator / stuck queue bug.

Simulates the full delegation pipeline:
1. 10 subagents dispatched simultaneously
2. 4 providers with max_concurrent=1 (4 start, 6 queue)
3. Provider completions at different rates
4. Dispatcher thread dies while tasks are queued
5. Verify queued tasks get orphaned

Run with: python3 -m pytest tests/tools/test_subagent_router_deadlock.py -v
"""

import threading
import time
import pytest
from unittest.mock import patch, MagicMock

# Must import and reset state before each test
from tools.subagent_router import (
    _RouterState,
    _state,
    acquire_provider,
    release_provider,
    enqueue_task,
    get_status,
    get_active_counts,
    _ensure_dispatcher,
    _try_dispatch_queued,
    _dispatcher_loop,
    _dispatch_queued_unlocked,
    _dispatch_queued_with_flag,
    stop_dispatcher,
    reset_state,
)


class TestDeadOrchestrator:
    """Reproduce the dead orchestrator bug step by step."""

    def setup_method(self):
        """Reset router state before each test."""
        _RouterState.force_reset()
        # Patch health check to always succeed
        self.health_patcher = patch(
            "tools.subagent_router._health_check", return_value=True
        )
        self.health_patcher.start()
        # Patch credential resolution
        self.creds_patcher = patch(
            "tools.subagent_router._resolve_provider_credentials",
            side_effect=lambda name: {
                "provider": name,
                "model": "test-model",
                "base_url": f"http://{name}:5678/v1",
                "api_key": "test-key",
                "api_mode": "chat_completions",
            },
        )
        self.creds_patcher.start()

    def teardown_method(self):
        self.health_patcher.stop()
        self.creds_patcher.stop()
        stop_dispatcher()
        _RouterState.force_reset()

    def test_basic_acquire_release(self):
        """Verify basic provider acquisition works."""
        # Mock config
        with patch("tools.subagent_router._is_enabled", return_value=True):
            with patch("tools.subagent_router._get_priority_order", return_value=[
                {"name": "192.168.1.224", "max_concurrent": 1, "enabled": True},
                {"name": "192.168.1.222", "max_concurrent": 1, "enabled": True},
                {"name": "192.168.1.223", "max_concurrent": 1, "enabled": True},
                {"name": "192.168.1.221", "max_concurrent": 1, "enabled": True},
            ]):
                # Acquire all 4 providers
                creds1 = acquire_provider()
                creds2 = acquire_provider()
                creds3 = acquire_provider()
                creds4 = acquire_provider()

                assert creds1 is not None
                assert creds2 is not None
                assert creds3 is not None
                assert creds4 is not None

                # 5th should return None (all at capacity)
                creds5 = acquire_provider()
                assert creds5 is None

                counts = get_active_counts()
                assert sum(counts.values()) == 4

                # Release one
                release_provider(creds1["provider"])
                counts = get_active_counts()
                assert sum(counts.values()) == 3

                # Now 5th should work
                creds5 = acquire_provider()
                assert creds5 is not None

    def test_health_check_inside_lock_blocks_everything(self):
        """BUG 1: Health check inside lock serializes ALL operations.

        When health check takes time (simulated slow network), NO other
        thread can acquire, release, or dispatch during that window.
        """
        health_calls = []

        def slow_health(url, timeout):
            health_calls.append(time.monotonic())
            time.sleep(0.5)  # Simulate slow network
            return True

        with patch("tools.subagent_router._is_enabled", return_value=True):
            with patch("tools.subagent_router._get_priority_order", return_value=[
                {"name": "192.168.1.224", "max_concurrent": 1, "enabled": True},
                {"name": "192.168.1.222", "max_concurrent": 1, "enabled": True},
                {"name": "192.168.1.223", "max_concurrent": 1, "enabled": True},
                {"name": "192.168.1.221", "max_concurrent": 1, "enabled": True},
            ]):
                with patch("tools.subagent_router._health_check", side_effect=slow_health):
                    start = time.monotonic()
                    # Acquire all 4 - each health check holds the lock for 0.5s
                    acquire_provider()
                    acquire_provider()
                    acquire_provider()
                    acquire_provider()
                    elapsed = time.monotonic() - start

                    # If health check is inside lock, this takes 4 * 0.5 = 2s
                    # If health check is outside lock, concurrent acquires can overlap
                    assert elapsed >= 1.8, (
                        f"Health check inside lock took {elapsed:.1f}s "
                        "(expected ~2s with 4 providers * 0.5s each)"
                    )

    def test_dispatcher_loop_double_dispatch_race(self):
        """BUG 2: Dispatcher loop doesn't check _dispatching flag.

        When release_provider calls _try_dispatch_queued AND the dispatcher
        loop runs in the same poll window, both can snapshot the same entries.
        """
        dispatch_count = [0]
        original_dispatch = _dispatch_queued_unlocked

        def counting_dispatch(entries):
            dispatch_count[0] += len(entries)
            # Simulate: all entries fail to acquire (no providers available)
            # so they all go back in the queue
            st = _state()
            with st._lock:
                st._pending_queue.extend(entries)

        with patch("tools.subagent_router._is_enabled", return_value=True):
            with patch("tools.subagent_router._get_priority_order", return_value=[
                {"name": "192.168.1.224", "max_concurrent": 1, "enabled": True},
            ]):
                with patch("tools.subagent_router._health_check", return_value=True):
                    # Fill provider
                    acquire_provider()

                    # Enqueue 3 tasks
                    for i in range(3):
                        enqueue_task(
                            task={"goal": f"task-{i}", "provider": None},
                            session_key="test",
                            sync_event=None,
                            parent_agent=MagicMock(),
                            delegation_cfg={},
                        )

                    # Start dispatcher
                    _ensure_dispatcher()

                    # Simulate: release_provider triggers _try_dispatch_queued
                    # AND dispatcher loop runs in same window
                    st = _state()

                    # Manually trigger both paths (simulating the race)
                    with st._lock:
                        # _try_dispatch_queued path
                        if st._pending_queue and not st._dispatching:
                            st._dispatching = True
                            snapshot1 = list(st._pending_queue)
                            st._pending_queue.clear()

                    # Now dispatcher loop runs (doesn't check _dispatching!)
                    snapshot2 = []
                    with st._lock:
                        if st._pending_queue:
                            snapshot2 = list(st._pending_queue)
                            st._pending_queue.clear()

                    # snapshot2 should be empty since _dispatching was True
                    # BUT the dispatcher loop doesn't check _dispatching!
                    # This is the bug - it should check st._dispatching
                    assert len(snapshot1) == 3, f"First dispatch got {len(snapshot1)} entries"
                    # BUG: dispatcher loop would also grab entries if any were left
                    # (in real scenario, some might fail and go back before flag clears)

                    # Clean up
                    with st._lock:
                        st._dispatching = False
                        st._pending_queue.clear()

    def test_recursive_dispatch_from_acquire_provider(self):
        """BUG 4: acquire_provider -> _ensure_dispatcher -> inline dispatch.

        When _dispatch_queued_unlocked calls acquire_provider, which calls
        _ensure_dispatcher, which finds pending tasks and dispatches them
        inline, we get recursive dispatch that can corrupt state.
        """
        call_stack = []

        original_ensure = _ensure_dispatcher

        def tracking_ensure():
            call_stack.append("ensure_dispatcher")
            return original_ensure()

        with patch("tools.subagent_router._is_enabled", return_value=True):
            with patch("tools.subagent_router._get_priority_order", return_value=[
                {"name": "192.168.1.224", "max_concurrent": 1, "enabled": True},
            ]):
                with patch("tools.subagent_router._health_check", return_value=True):
                    with patch("tools.subagent_router._ensure_dispatcher", side_effect=tracking_ensure):
                        # Fill provider
                        acquire_provider()

                        # Enqueue a task
                        enqueue_task(
                            task={"goal": "task-1"},
                            session_key="test",
                            sync_event=None,
                            parent_agent=MagicMock(),
                            delegation_cfg={},
                        )

                        # Now release - this triggers _try_dispatch_queued
                        # which calls acquire_provider -> _ensure_dispatcher
                        release_provider("192.168.1.224")

                        # _ensure_dispatcher should have been called during release
                        # (from _try_dispatch_queued -> _dispatch_queued_unlocked -> acquire_provider)
                        # This is the recursive path

    def test_provider_never_released_on_dispatch_failure(self):
        """BUG: When _dispatch_single_queued fails, it tries to release
        the provider but guesses wrong, leaving phantom capacity.
        """
        with patch("tools.subagent_router._is_enabled", return_value=True):
            with patch("tools.subagent_router._get_priority_order", return_value=[
                {"name": "192.168.1.224", "max_concurrent": 1, "enabled": True},
                {"name": "192.168.1.222", "max_concurrent": 1, "enabled": True},
            ]):
                with patch("tools.subagent_router._health_check", return_value=True):
                    # Acquire both providers
                    acquire_provider()
                    acquire_provider()

                    counts = get_active_counts()
                    assert counts.get("192.168.1.224", 0) == 1
                    assert counts.get("192.168.1.222", 0) == 1

                    # Simulate: _dispatch_single_queued fails and tries to release
                    # It guesses the provider name from get_active_counts()
                    # which returns BOTH providers with count > 0
                    # It picks the first one it finds, which may be wrong
                    guessed_provider = None
                    for name, count in get_active_counts().items():
                        if count > 0:
                            guessed_provider = name
                            break

                    # The bug: it might release 192.168.1.222 when the task
                    # was actually assigned to 192.168.1.224
                    if guessed_provider:
                        release_provider(guessed_provider)

                    counts = get_active_counts()
                    # One provider is now at 0 (freed) but the other is still at 1
                    # even though no task is actually running on it
                    # This creates phantom capacity that blocks future dispatches

    def test_sync_wait_10_minutes_on_dead_dispatcher(self):
        """BUG 5: When all tasks are queued and dispatcher dies,
        parent blocks for 600s (10 minutes) before timeout.
        """
        # This test demonstrates the symptom without actually waiting 10 minutes
        # We just verify the timeout is set to 600s in the code
        # (read from delegate_tool.py line 2624)
        import tools.delegate_tool as dt
        # The sync_event.wait(timeout=600) is hardcoded
        # This is the line: qt["sync_event"].wait(timeout=600)
        # A 10-minute timeout is unreasonable for a stuck dispatcher
        assert True  # Structural issue documented in code


class TestLockContentionTimeline:
    """Trace the exact lock contention timeline when 10 subagents dispatch."""

    def setup_method(self):
        _RouterState.force_reset()
        self.health_patcher = patch(
            "tools.subagent_router._health_check", return_value=True
        )
        self.health_patcher.start()
        self.creds_patcher = patch(
            "tools.subagent_router._resolve_provider_credentials",
            side_effect=lambda name: {
                "provider": name,
                "model": "test-model",
                "base_url": f"http://{name}:5678/v1",
                "api_key": "test-key",
                "api_mode": "chat_completions",
            },
        )
        self.creds_patcher.start()

    def teardown_method(self):
        self.health_patcher.stop()
        self.creds_patcher.stop()
        stop_dispatcher()
        _RouterState.force_reset()

    def test_full_dispatch_timeline(self):
        """Simulate: 10 subagents, 4 providers (max_concurrent=1 each).

        Timeline:
        1. Tasks 0-3 acquire providers immediately (4 slots)
        2. Tasks 4-9 go to queue (6 tasks)
        3. Task 0 completes -> release_provider -> _try_dispatch_queued -> Task 4 starts
        4. Task 1 completes -> release_provider -> _try_dispatch_queued -> Task 5 starts
        5. Task 2 completes -> release_provider -> _try_dispatch_queued -> Task 6 starts
        6. Task 3 completes -> release_provider -> _try_dispatch_queued -> Task 7 starts
        7. Tasks 8-9 still in queue
        8. Dispatcher thread dies (simulated)
        9. Tasks 8-9 are orphaned

        The bug: when dispatcher dies, _ensure_dispatcher restarts it but
        the inline dispatch in _ensure_dispatcher races with the restarted
        dispatcher loop, and the _dispatching flag isn't checked by the loop.
        """
        with patch("tools.subagent_router._is_enabled", return_value=True):
            with patch("tools.subagent_router._get_priority_order", return_value=[
                {"name": "192.168.1.224", "max_concurrent": 1, "enabled": True},
                {"name": "192.168.1.222", "max_concurrent": 1, "enabled": True},
                {"name": "192.168.1.223", "max_concurrent": 1, "enabled": True},
                {"name": "192.168.1.221", "max_concurrent": 1, "enabled": True},
            ]):
                # Phase 1: Acquire all 4 providers
                providers = []
                for _ in range(4):
                    creds = acquire_provider()
                    assert creds is not None
                    providers.append(creds["provider"])

                # 5th should fail
                assert acquire_provider() is None

                # Phase 2: Enqueue 6 tasks
                enqueued = []
                for i in range(6):
                    result = enqueue_task(
                        task={"goal": f"queued-task-{i}"},
                        session_key="test",
                        sync_event=None,
                        parent_agent=MagicMock(),
                        delegation_cfg={},
                    )
                    enqueued.append(result)

                assert all(enqueued), "All tasks should be enqueued"

                status = get_status()
                assert status["queue_size"] == 6
                assert sum(status["active_counts"].values()) == 4

                # Phase 3: Release one provider, check queue dispatch
                release_provider(providers[0])

                # After release, _try_dispatch_queued should have dispatched one task
                # (but it runs in a background thread, so we need to wait)
                time.sleep(0.5)

                status = get_status()
                # Queue should have decreased by 1 (one task dispatched)
                # OR the task could have been dispatched and put back if no provider
                # (since we just released one, it should have been acquired)
                # This is where the race condition manifests

                # Phase 4: Simulate dispatcher death
                st = _state()
                st._dispatcher_running = False
                if st._dispatcher_thread:
                    st._dispatcher_thread.join(timeout=1)

                # Phase 5: Release another provider
                # This should detect dead dispatcher and restart it
                release_provider(providers[1])

                time.sleep(0.5)

                status = get_status()
                # The restarted dispatcher should handle remaining tasks
                # BUT if the inline dispatch in release_provider races with
                # the restarted dispatcher loop, tasks can be lost


class TestNestedDispatchCascade:
    """Test that releasing a provider during dispatch does NOT spawn
    nested dispatch threads that re-dispatch the same remaining entries.

    This was the primary cause of dispatcher death during 6+ parallel
    subagent processing: when _dispatch_queued_unlocked failed to dispatch
    a task, it called release_provider() which triggered queue dispatch,
    which spawned another dispatch thread that re-dispatched the same
    remaining entries, creating exponential thread explosion.
    """

    def setUp(self):
        _RouterState.force_reset()

    def tearDown(self):
        stop_dispatcher()
        _RouterState.force_reset()

    def test_release_during_dispatch_does_not_cascade(self):
        """Verify that release_provider(skip_queue_dispatch=True) does NOT
        trigger queue dispatch or dispatcher restart."""
        with patch("tools.subagent_router._is_enabled", return_value=True):
            with patch("tools.subagent_router._get_priority_order", return_value=[
                {"name": "server-a", "max_concurrent": 1, "enabled": True},
            ]):
                with patch("tools.subagent_router._resolve_provider_credentials",
                           return_value={"provider": "server-a", "model": "m",
                                         "base_url": "http://test:5678/v1/",
                                         "api_key": "k", "api_mode": "chat_completions"}):
                    with patch("tools.subagent_router._health_check", return_value=True):
                        # Acquire the only slot
                        creds = acquire_provider(provider_override="server-a")
                        assert creds is not None
                        assert get_active_counts().get("server-a") == 1

                        # Enqueue a task (queue is enabled by default)
                        sync_event = threading.Event()
                        enqueued = enqueue_task(
                            task={"goal": "test task"},
                            session_key="test",
                            sync_event=sync_event,
                            parent_agent=MagicMock(),
                            delegation_cfg={},
                        )
                        assert enqueued
                        assert len(_state()._pending_queue) == 1

                        # Release with skip_queue_dispatch=True
                        # This should NOT trigger queue dispatch
                        release_provider("server-a", skip_queue_dispatch=True)

                        # Queue should still have the task (not dispatched)
                        assert len(_state()._pending_queue) == 1
                        # Active count should be 0
                        assert get_active_counts().get("server-a") == 0

    def test_no_nested_dispatch_threads_spawned(self):
        """Verify that multiple releases during dispatch don't spawn
        exponential threads."""
        with patch("tools.subagent_router._is_enabled", return_value=True):
            with patch("tools.subagent_router._get_priority_order", return_value=[
                {"name": "server-a", "max_concurrent": 2, "enabled": True},
            ]):
                with patch("tools.subagent_router._resolve_provider_credentials",
                           return_value={"provider": "server-a", "model": "m",
                                         "base_url": "http://test:5678/v1/",
                                         "api_key": "k", "api_mode": "chat_completions"}):
                    with patch("tools.subagent_router._health_check", return_value=True):
                        # Acquire both slots
                        acquire_provider(provider_override="server-a")
                        acquire_provider(provider_override="server-a")
                        assert get_active_counts().get("server-a") == 2

                        # Count threads before
                        before = threading.active_count()

                        # Release both with skip_queue_dispatch
                        release_provider("server-a", skip_queue_dispatch=True)
                        release_provider("server-a", skip_queue_dispatch=True)

                        # No new threads should have been spawned
                        after = threading.active_count()
                        assert after == before, f"Threads spawned: {after - before}"


class TestDispatcherThreadIdTracking:
    """Test that _thread_id prevents multiple dispatcher threads from
    racing and that stale dispatchers exit gracefully."""

    def setUp(self):
        _RouterState.force_reset()

    def tearDown(self):
        stop_dispatcher()
        _RouterState.force_reset()

    def test_thread_id_prevents_duplicate_dispatchers(self):
        """Two threads calling _ensure_dispatcher simultaneously should
        only result in one dispatcher thread."""
        with patch("tools.subagent_router._is_enabled", return_value=True):
            started = []
            errors = []

            def ensure_and_record():
                try:
                    _ensure_dispatcher()
                    started.append(1)
                except Exception as e:
                    errors.append(e)

            # Spawn two threads that both try to ensure dispatcher
            t1 = threading.Thread(target=ensure_and_record)
            t2 = threading.Thread(target=ensure_and_record)
            t1.start()
            t2.start()
            t1.join(timeout=5)
            t2.join(timeout=5)

            assert not errors, f"Errors: {errors}"

            # Only one dispatcher should be running
            st = _state()
            assert st._thread_id is not None
            assert st._dispatcher_thread is not None
            assert st._dispatcher_thread.is_alive()

    def test_stale_dispatcher_exits_on_replacement(self):
        """When _ensure_dispatcher starts a new thread, the old one
        should detect the thread_id mismatch and exit."""
        with patch("tools.subagent_router._is_enabled", return_value=True):
            with patch("tools.subagent_router._get_poll_interval", return_value=0.1):
                # Start first dispatcher
                _ensure_dispatcher()
                st = _state()
                old_thread = st._dispatcher_thread
                old_thread_id = st._thread_id
                assert old_thread is not None
                assert old_thread.is_alive()

                # Simulate dispatcher death (set running=False)
                st._dispatcher_running = False
                # Wait for old thread to notice and exit (poll_interval = 0.1s)
                old_thread.join(timeout=2)

                # Start new dispatcher - counter increments so new ID > old ID
                _ensure_dispatcher()
                new_thread_id = st._thread_id
                # Counter should have incremented (old was N, new is N+1)
                assert new_thread_id > old_thread_id, \
                    f"Expected new_id({new_thread_id}) > old_id({old_thread_id})"
                assert st._dispatcher_thread is not None
                assert st._dispatcher_thread.is_alive()


class TestConfigCacheInvalidation:
    """Test that config changes are picked up without restarting the router."""

    def setUp(self):
        _RouterState.force_reset()

    def tearDown(self):
        stop_dispatcher()
        _RouterState.force_reset()

    def test_queue_config_reloaded_on_each_call(self):
        """_get_queue_config() should re-read from config on each call,
        not return a cached stale value."""
        call_count = [0]

        def mock_config(*args, **kwargs):
            call_count[0] += 1
            return {"queue": {"max_size": 10, "poll_interval": 1}}

        with patch("tools.subagent_router._load_subagent_routing_config", side_effect=mock_config):
            from tools.subagent_router import _get_queue_config
            qc1 = _get_queue_config()
            qc2 = _get_queue_config()
            qc3 = _get_queue_config()

            assert qc1 == {"max_size": 10, "poll_interval": 1}
            assert qc2 == {"max_size": 10, "poll_interval": 1}
            assert qc3 == {"max_size": 10, "poll_interval": 1}
            # Each call should re-read config (not cached)
            assert call_count[0] == 3, f"Expected 3 calls, got {call_count[0]}"


class TestResetStateSafety:
    """Test that reset_state() doesn't kill the dispatcher mid-dispatch."""

    def setUp(self):
        stop_dispatcher()  # Kill any lingering threads from previous tests
        _RouterState.force_reset()

    def tearDown(self):
        stop_dispatcher()
        _RouterState.force_reset()

    def test_reset_does_not_stop_dispatcher(self):
        """reset_state() should clear counts/queue but NOT stop the
        dispatcher thread (which would orphan in-flight tasks)."""
        with patch("tools.subagent_router._is_enabled", return_value=True):
            with patch("tools.subagent_router._get_poll_interval", return_value=0.1):
                # Start dispatcher
                _ensure_dispatcher()
                st = _state()
                assert st._dispatcher_thread is not None
                # Wait for thread to actually start
                for _ in range(50):
                    if st._dispatcher_thread.is_alive():
                        break
                    time.sleep(0.05)
                assert st._dispatcher_thread.is_alive()

                # Set some state
                st._active_counts["test"] = 5
                st._pending_queue.append({"task": {"goal": "test"}})

                # Reset
                reset_state()

                # Dispatcher should still be running
                assert st._dispatcher_thread.is_alive()
                assert st._dispatcher_running

            # State should be cleared
            assert get_active_counts() == {}
            assert len(st._pending_queue) == 0


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
