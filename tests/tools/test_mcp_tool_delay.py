"""Tests for the MCP tool call delay mechanism (thundering-herd prevention).

Post-merge the delay logic lives in ``tools.mcp_tool_handlers`` (it was re-homed
from ``tools.mcp_tool`` during the upstream merge). Config is read via
``hermes_cli.config.load_config`` and cached on the module-level
``_mcp_delay_cache`` / ``_MCP_DELAY_CACHE_TTL``.
"""

import fnmatch
import time
from unittest.mock import MagicMock, patch


class TestMcpDelayPatternMatching:
    """Verify fnmatch pattern matching for tool name filtering."""

    def test_search_tool_matches_default_pattern(self):
        patterns = ["*search*", "*fetch*"]
        assert any(fnmatch.fnmatch("mcp-open-websearch/search", p) for p in patterns)

    def test_fetch_web_content_matches_default_pattern(self):
        patterns = ["*search*", "*fetch*"]
        assert any(fnmatch.fnmatch("mcp-open-websearch/fetchWebContent", p) for p in patterns)

    def test_fetch_csdn_matches_default_pattern(self):
        patterns = ["*search*", "*fetch*"]
        assert any(fnmatch.fnmatch("mcp-open-websearch/fetchCsdnArticle", p) for p in patterns)

    def test_fetch_github_readme_matches_default_pattern(self):
        patterns = ["*search*", "*fetch*"]
        assert any(fnmatch.fnmatch("mcp-open-websearch/fetchGithubReadme", p) for p in patterns)

    def test_filesystem_tool_does_not_match(self):
        patterns = ["*search*", "*fetch*"]
        tool_name = "mcp-filesystem/read_file"
        assert not any(fnmatch.fnmatch(tool_name, p) for p in patterns)

    def test_terminal_tool_does_not_match(self):
        patterns = ["*search*", "*fetch*"]
        tool_name = "terminal"
        assert not any(fnmatch.fnmatch(tool_name, p) for p in patterns)

    def test_execute_code_does_not_match(self):
        patterns = ["*search*", "*fetch*"]
        tool_name = "execute_code"
        assert not any(fnmatch.fnmatch(tool_name, p) for p in patterns)

    def test_custom_pattern_matches(self):
        patterns = ["*custom*"]
        assert any(fnmatch.fnmatch("mcp-custom/myTool", p) for p in patterns)

    def test_bare_tool_name_matching(self):
        """Bare tool name (without server prefix) should also match."""
        patterns = ["*search*", "*fetch*"]
        assert any(fnmatch.fnmatch("search", p) for p in patterns)
        assert any(fnmatch.fnmatch("fetchWebContent", p) for p in patterns)


def _reset_delay_cache():
    import tools.mcp_tool_handlers as h
    h._mcp_delay_cache = (0.0, 0.0, 0, [])


class TestMcpDelayConfigReading:
    """Verify _get_mcp_delay_config reads and caches config correctly."""

    def test_delay_config_returns_zero_when_not_set(self):
        """Default delay is 0 (disabled) when config has no agent.tool_call_delay_ms."""
        with patch("hermes_cli.config.load_config", return_value={}):
            _reset_delay_cache()
            from tools.mcp_tool_handlers import _get_mcp_delay_config
            delay_ms, patterns = _get_mcp_delay_config()
            assert delay_ms == 0
            assert patterns == ["*search*", "*fetch*"]

    def test_delay_config_reads_value_from_config(self):
        """Delay value from config is returned."""
        with patch("hermes_cli.config.load_config", return_value={
            "agent": {"tool_call_delay_ms": 500}
        }):
            _reset_delay_cache()
            from tools.mcp_tool_handlers import _get_mcp_delay_config
            delay_ms, patterns = _get_mcp_delay_config()
            assert delay_ms == 500

    def test_delay_config_reads_custom_patterns(self):
        """Custom patterns from config override defaults."""
        with patch("hermes_cli.config.load_config", return_value={
            "agent": {
                "tool_call_delay_ms": 1000,
                "mcp_delay_tool_patterns": ["*web*", "*crawl*"]
            }
        }):
            _reset_delay_cache()
            from tools.mcp_tool_handlers import _get_mcp_delay_config
            delay_ms, patterns = _get_mcp_delay_config()
            assert delay_ms == 1000
            assert patterns == ["*web*", "*crawl*"]

    def test_delay_config_caching(self):
        """Config is cached and not re-read within TTL."""
        mock_load = MagicMock(return_value={"agent": {"tool_call_delay_ms": 200}})
        with patch("hermes_cli.config.load_config", mock_load):
            _reset_delay_cache()
            from tools.mcp_tool_handlers import _get_mcp_delay_config
            _get_mcp_delay_config()
            _get_mcp_delay_config()
            _get_mcp_delay_config()
            # Should only be called once due to caching
            mock_load.assert_called_once()

    def test_delay_config_cache_expiry(self):
        """Config is re-read after TTL expires."""
        import tools.mcp_tool_handlers as h
        original_ttl = h._MCP_DELAY_CACHE_TTL
        try:
            mock_load = MagicMock(return_value={"agent": {"tool_call_delay_ms": 300}})
            with patch("hermes_cli.config.load_config", mock_load):
                _reset_delay_cache()
                h._MCP_DELAY_CACHE_TTL = 0.001  # 1ms TTL
                from tools.mcp_tool_handlers import _get_mcp_delay_config
                _get_mcp_delay_config()
                time.sleep(0.01)  # Wait past TTL
                _get_mcp_delay_config()
                # Should be called twice (once before expiry, once after)
                assert mock_load.call_count == 2
        finally:
            h._MCP_DELAY_CACHE_TTL = original_ttl

    def test_delay_config_graceful_on_load_error(self):
        """If load_config raises, delay defaults to 0 with no crash."""
        with patch("hermes_cli.config.load_config", side_effect=Exception("config broken")):
            _reset_delay_cache()
            from tools.mcp_tool_handlers import _get_mcp_delay_config
            delay_ms, patterns = _get_mcp_delay_config()
            assert delay_ms == 0
            assert patterns == ["*search*", "*fetch*"]


class TestShouldDelayTool:
    """Verify _should_delay_tool checks patterns correctly."""

    def test_search_tool_should_delay(self):
        with patch("tools.mcp_tool_handlers._get_mcp_delay_config", return_value=(1000, ["*search*"])):
            from tools.mcp_tool_handlers import _should_delay_tool
            assert _should_delay_tool("search") is True

    def test_non_matching_tool_should_not_delay(self):
        with patch("tools.mcp_tool_handlers._get_mcp_delay_config", return_value=(1000, ["*search*"])):
            from tools.mcp_tool_handlers import _should_delay_tool
            assert _should_delay_tool("read_file") is False


class TestMcpToolHandlerDelayIntegration:
    """Verify the delay is applied in _make_tool_handler when conditions are met.

    The delay hook runs at the very top of the handler, BEFORE the trust gate.
    We mock ``_trust_gate_check`` to short-circuit right after the delay so the
    test never touches live server / circuit-breaker state.
    """

    def _run_handler(self, server_name, tool_name, delay_cfg):
        with patch("tools.mcp_tool_handlers._get_mcp_delay_config", return_value=delay_cfg):
            with patch("time.sleep") as mock_sleep:
                with patch("tools.mcp_tool_handlers._trust_gate_check", return_value="[circuit]"):
                    from tools.mcp_tool_handlers import _make_tool_handler
                    handler = _make_tool_handler(server_name, tool_name, 300)
                    result = handler({})
        return result, mock_sleep

    def test_handler_applies_delay_for_matching_tool(self):
        """When delay_ms > 0 and tool matches pattern, time.sleep is called."""
        result, mock_sleep = self._run_handler("open-websearch", "search", (500, ["*search*"]))
        # Short-circuited by the mocked trust gate, but AFTER the delay fired.
        assert result == "[circuit]"
        mock_sleep.assert_called_once_with(0.5)

    def test_handler_skips_delay_for_non_matching_tool(self):
        """When tool doesn't match pattern, no sleep occurs."""
        result, mock_sleep = self._run_handler("filesystem", "read_file", (500, ["*search*"]))
        assert result == "[circuit]"
        mock_sleep.assert_not_called()

    def test_handler_skips_delay_when_delay_is_zero(self):
        """When delay_ms is 0, no sleep regardless of tool name."""
        result, mock_sleep = self._run_handler("open-websearch", "search", (0, ["*search*"]))
        assert result == "[circuit]"
        mock_sleep.assert_not_called()
