#!/usr/bin/env python3
"""Tests for tools/subagent_router.py — priority-based subagent routing.

These tests exercise the routing engine in isolation:
  - acquire/release with concurrency counters
  - priority ordering and enabled/disabled servers
  - health check pass/fail
  - goal_rules matching (substring and regex)
  - per-task provider override
  - overflow queue enqueue/dispatch
  - state reset
"""

import threading
import time
import unittest
from unittest.mock import MagicMock, patch

# Import the module under test
from tools.subagent_router import (
    acquire_provider,
    enqueue_task,
    get_active_counts,
    get_status,
    release_provider,
    reset_state,
    stop_dispatcher,
    _active_counts,
    _is_enabled,
    _load_subagent_routing_config,
    _match_goal_rule,
    _pending_queue,
    _resolve_provider_credentials,
)


class TestSubagentRouterState(unittest.TestCase):
    """State management: reset, active counts, status."""

    def setUp(self):
        reset_state()

    def tearDown(self):
        reset_state()

    def test_initial_state_is_empty(self):
        self.assertEqual(get_active_counts(), {})

    def test_acquire_increments_count(self):
        with patch("tools.subagent_router._is_enabled", return_value=False):
            # Explicit override path doesn't need routing enabled
            with patch(
                "tools.subagent_router._resolve_provider_credentials",
                return_value={
                    "provider": "test",
                    "model": "test-model",
                    "base_url": "http://test:5678/v1/",
                    "api_key": "key",
                },
            ):
                creds = acquire_provider(provider_override="test-server")
                self.assertIsNotNone(creds)
                counts = get_active_counts()
                self.assertEqual(counts.get("test-server"), 1)

    def test_release_decrements_count(self):
        with patch("tools.subagent_router._is_enabled", return_value=False):
            with patch(
                "tools.subagent_router._resolve_provider_credentials",
                return_value={
                    "provider": "test",
                    "model": "test-model",
                    "base_url": "http://test:5678/v1/",
                    "api_key": "key",
                },
            ):
                acquire_provider(provider_override="test-server")
                release_provider("test-server")
                counts = get_active_counts()
                self.assertEqual(counts.get("test-server"), 0)

    def test_reset_clears_state(self):
        _active_counts["some-provider"] = 5
        reset_state()
        self.assertEqual(get_active_counts(), {})
        self.assertEqual(len(_pending_queue), 0)

    def test_status_report(self):
        status = get_status()
        self.assertIn("enabled", status)
        self.assertIn("active_counts", status)
        self.assertIn("queue_size", status)
        self.assertIn("queue_max", status)
        self.assertIn("priority_order", status)


class TestGoalRules(unittest.TestCase):
    """Goal-based rule matching (substring and regex)."""

    def setUp(self):
        reset_state()

    def test_substring_match(self):
        with patch(
            "tools.subagent_router._load_subagent_routing_config",
            return_value={
                "goal_rules": [
                    {"match": "code review", "provider": "server-a"},
                    {"match": "research", "provider": "server-b"},
                ]
            },
        ):
            result = _match_goal_rule("Please do a quick code review")
            self.assertEqual(result, "server-a")

            result = _match_goal_rule("Research the latest papers")
            self.assertEqual(result, "server-b")

            result = _match_goal_rule("Write a blog post")
            self.assertIsNone(result)

    def test_regex_match(self):
        with patch(
            "tools.subagent_router._load_subagent_routing_config",
            return_value={
                "goal_rules": [
                    {"match": "^test.*", "provider": "test-server"},
                ]
            },
        ):
            result = _match_goal_rule("test this model")
            self.assertEqual(result, "test-server")

            result = _match_goal_rule("not a test")
            self.assertIsNone(result)

    def test_case_insensitive(self):
        with patch(
            "tools.subagent_router._load_subagent_routing_config",
            return_value={
                "goal_rules": [
                    {"match": "code review", "provider": "server-a"},
                ]
            },
        ):
            result = _match_goal_rule("CODE REVIEW please")
            self.assertEqual(result, "server-a")

    def test_no_rules_returns_none(self):
        with patch(
            "tools.subagent_router._load_subagent_routing_config",
            return_value={},
        ):
            result = _match_goal_rule("anything")
            self.assertIsNone(result)


class TestAcquireProvider(unittest.TestCase):
    """Provider acquisition: priority order, capacity, health checks."""

    def setUp(self):
        reset_state()

    def tearDown(self):
        reset_state()

    def test_explicit_override_ignores_routing(self):
        """Per-task provider override should work regardless of routing enabled."""
        with patch("tools.subagent_router._is_enabled", return_value=False):
            with patch(
                "tools.subagent_router._resolve_provider_credentials",
                return_value={
                    "provider": "custom",
                    "model": "test-model",
                    "base_url": "http://192.168.1.224:5678/v1/",
                    "api_key": "key",
                },
            ):
                creds = acquire_provider(provider_override="192.168.1.224")
                self.assertIsNotNone(creds)
                self.assertEqual(creds["model"], "test-model")

    def test_priority_order_first_available(self):
        """Should pick the first enabled server with capacity."""
        with patch("tools.subagent_router._is_enabled", return_value=True):
            with patch(
                "tools.subagent_router._get_priority_order",
                return_value=[
                    {"name": "server-a", "enabled": True, "max_concurrent": 1},
                    {"name": "server-b", "enabled": True, "max_concurrent": 1},
                ],
            ):
                with patch(
                    "tools.subagent_router._resolve_provider_credentials",
                    return_value={
                        "provider": "custom",
                        "model": "model",
                        "base_url": "http://server-a:5678/v1/",
                        "api_key": "key",
                    },
                ):
                    with patch(
                        "tools.subagent_router._health_check", return_value=True
                    ):
                        creds = acquire_provider()
                        self.assertIsNotNone(creds)
                        counts = get_active_counts()
                        self.assertEqual(counts.get("server-a"), 1)

    def test_skip_disabled_server(self):
        """Should skip servers with enabled: false."""
        with patch("tools.subagent_router._is_enabled", return_value=True):
            with patch(
                "tools.subagent_router._get_priority_order",
                return_value=[
                    {"name": "disabled-server", "enabled": False, "max_concurrent": 1},
                    {"name": "enabled-server", "enabled": True, "max_concurrent": 1},
                ],
            ):
                with patch(
                    "tools.subagent_router._resolve_provider_credentials",
                    return_value={
                        "provider": "custom",
                        "model": "model",
                        "base_url": "http://enabled-server:5678/v1/",
                        "api_key": "key",
                    },
                ):
                    with patch(
                        "tools.subagent_router._health_check", return_value=True
                    ):
                        creds = acquire_provider()
                        self.assertIsNotNone(creds)
                        counts = get_active_counts()
                        self.assertNotIn("disabled-server", counts)
                        self.assertEqual(counts.get("enabled-server"), 1)

    def test_skip_at_capacity(self):
        """Should skip servers that are at max_concurrent."""
        with patch("tools.subagent_router._is_enabled", return_value=True):
            with patch(
                "tools.subagent_router._get_priority_order",
                return_value=[
                    {"name": "full-server", "enabled": True, "max_concurrent": 1},
                    {"name": "available-server", "enabled": True, "max_concurrent": 1},
                ],
            ):
                # Pre-fill the first server to capacity
                _active_counts["full-server"] = 1

                with patch(
                    "tools.subagent_router._resolve_provider_credentials",
                    return_value={
                        "provider": "custom",
                        "model": "model",
                        "base_url": "http://available-server:5678/v1/",
                        "api_key": "key",
                    },
                ):
                    with patch(
                        "tools.subagent_router._health_check", return_value=True
                    ):
                        creds = acquire_provider()
                        self.assertIsNotNone(creds)
                        counts = get_active_counts()
                        self.assertEqual(counts.get("available-server"), 1)

    def test_skip_missing_max_concurrent(self):
        """Should skip servers that don't have max_concurrent set."""
        with patch("tools.subagent_router._is_enabled", return_value=True):
            with patch(
                "tools.subagent_router._get_priority_order",
                return_value=[
                    {"name": "no-limit-server", "enabled": True},
                    {"name": "good-server", "enabled": True, "max_concurrent": 1},
                ],
            ):
                with patch(
                    "tools.subagent_router._resolve_provider_credentials",
                    return_value={
                        "provider": "custom",
                        "model": "model",
                        "base_url": "http://good-server:5678/v1/",
                        "api_key": "key",
                    },
                ):
                    with patch(
                        "tools.subagent_router._health_check", return_value=True
                    ):
                        creds = acquire_provider()
                        self.assertIsNotNone(creds)
                        counts = get_active_counts()
                        self.assertNotIn("no-limit-server", counts)

    def test_health_check_failure_skips_server(self):
        """Should skip servers that fail health check."""
        def health_check_side_effect(base_url, timeout):
            return "good-server" in base_url

        with patch("tools.subagent_router._is_enabled", return_value=True):
            with patch(
                "tools.subagent_router._get_priority_order",
                return_value=[
                    {"name": "bad-server", "enabled": True, "max_concurrent": 1},
                    {"name": "good-server", "enabled": True, "max_concurrent": 1},
                ],
            ):
                with patch(
                    "tools.subagent_router._resolve_provider_credentials",
                    side_effect=[
                        {
                            "provider": "custom",
                            "model": "model",
                            "base_url": "http://bad-server:5678/v1/",
                            "api_key": "key",
                        },
                        {
                            "provider": "custom",
                            "model": "model",
                            "base_url": "http://good-server:5678/v1/",
                            "api_key": "key",
                        },
                    ],
                ):
                    with patch(
                        "tools.subagent_router._health_check",
                        side_effect=health_check_side_effect,
                    ):
                        creds = acquire_provider()
                        self.assertIsNotNone(creds)
                        counts = get_active_counts()
                        self.assertNotIn("bad-server", counts)
                        self.assertEqual(counts.get("good-server"), 1)

    def test_returns_none_when_all_full(self):
        """Should return None when all servers are at capacity."""
        with patch("tools.subagent_router._is_enabled", return_value=True):
            with patch(
                "tools.subagent_router._get_priority_order",
                return_value=[
                    {"name": "server-a", "enabled": True, "max_concurrent": 1},
                ],
            ):
                _active_counts["server-a"] = 1
                creds = acquire_provider()
                self.assertIsNone(creds)

    def test_multi_concurrent_support(self):
        """Server with max_concurrent=2 should accept 2 subagents."""
        with patch("tools.subagent_router._is_enabled", return_value=True):
            with patch(
                "tools.subagent_router._get_priority_order",
                return_value=[
                    {"name": "big-server", "enabled": True, "max_concurrent": 2},
                ],
            ):
                with patch(
                    "tools.subagent_router._resolve_provider_credentials",
                    return_value={
                        "provider": "custom",
                        "model": "model",
                        "base_url": "http://big-server:5678/v1/",
                        "api_key": "key",
                    },
                ):
                    with patch(
                        "tools.subagent_router._health_check", return_value=True
                    ):
                        # First acquisition
                        creds1 = acquire_provider()
                        self.assertIsNotNone(creds1)

                        # Second acquisition (still under limit)
                        creds2 = acquire_provider()
                        self.assertIsNotNone(creds2)

                        counts = get_active_counts()
                        self.assertEqual(counts.get("big-server"), 2)

                        # Third acquisition should fail (at capacity)
                        creds3 = acquire_provider()
                        self.assertIsNone(creds3)


class TestQueue(unittest.TestCase):
    """Overflow queue: enqueue, capacity, state."""

    def setUp(self):
        reset_state()

    def tearDown(self):
        reset_state()

    def test_enqueue_returns_true_when_space(self):
        with patch("tools.subagent_router._is_queue_enabled", return_value=True):
            with patch("tools.subagent_router._get_queue_max_size", return_value=20):
                with patch("tools.subagent_router._ensure_dispatcher"):
                    result = enqueue_task(
                        task={"goal": "test task"},
                        session_key="test-session",
                        sync_event=None,
                        parent_agent=MagicMock(),
                        delegation_cfg={"max_iterations": 50},
                    )
                    self.assertTrue(result)
                    self.assertEqual(len(_pending_queue), 1)

    def test_enqueue_returns_false_when_full(self):
        with patch("tools.subagent_router._is_queue_enabled", return_value=True):
            with patch("tools.subagent_router._get_queue_max_size", return_value=1):
                with patch("tools.subagent_router._ensure_dispatcher"):
                    # Fill the queue
                    enqueue_task(
                        task={"goal": "task 1"},
                        session_key="test-session",
                        sync_event=None,
                        parent_agent=MagicMock(),
                        delegation_cfg={},
                    )
                    # Should fail
                    result = enqueue_task(
                        task={"goal": "task 2"},
                        session_key="test-session",
                        sync_event=None,
                        parent_agent=MagicMock(),
                        delegation_cfg={},
                    )
                    self.assertFalse(result)

    def test_enqueue_disabled_returns_false(self):
        with patch("tools.subagent_router._is_queue_enabled", return_value=False):
            result = enqueue_task(
                task={"goal": "test"},
                session_key="test",
                sync_event=None,
                parent_agent=MagicMock(),
                delegation_cfg={},
            )
            self.assertFalse(result)

    def test_queue_entry_captures_context(self):
        with patch("tools.subagent_router._is_queue_enabled", return_value=True):
            with patch("tools.subagent_router._get_queue_max_size", return_value=20):
                with patch("tools.subagent_router._ensure_dispatcher"):
                    parent_agent = MagicMock()
                    sync_event = threading.Event()
                    enqueue_task(
                        task={"goal": "test task", "provider": "override"},
                        session_key="my-session",
                        sync_event=sync_event,
                        parent_agent=parent_agent,
                        delegation_cfg={"max_iterations": 100},
                    )
            result = _pending_queue[0]
            self.assertIsNotNone(result)
            self.assertEqual(result["task"]["goal"], "test task")
            self.assertEqual(result["session_key"], "my-session")
            self.assertEqual(result["sync_event"], sync_event)
            self.assertEqual(result["parent_agent"], parent_agent)
            self.assertEqual(result["delegation_cfg"]["max_iterations"], 100)


if __name__ == "__main__":
    unittest.main()
