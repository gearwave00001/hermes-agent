"""Tests for the MCP tool call delay mechanism (thundering-herd prevention)."""

import fnmatch
import pytest
from unittest.mock import patch, MagicMock


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


class TestMcpDelayConfigReading:
    """Verify _get_mcp_delay_config reads and caches config correctly."""

    def test_delay_config_returns_zero_when_not_set(self):
        """Default delay is 0 (disabled) when config has no agent.tool_call_delay_ms."""
        with patch("tools.mcp_tool.load_config", return_value={}):
            # Reset cache so we force a config read
            import tools.mcp_tool as mcp_mod
            mcp_mod._mcp_delay_cache = (0.0, 0.0, 0, [])

            from tools.mcp_tool import _get_mcp_delay_config
            delay_ms, patterns = _get_mcp_delay_config()
            assert delay_ms == 0
            assert patterns == ["*search*", "*fetch*"]

    def test_delay_config_reads_value_from_config(self):
        """Delay value from config is returned."""
        with patch("tools.mcp_tool.load_config", return_value={
            "agent": {"tool_call_delay_ms": 500}
        }):
            import tools.mcp_tool as mcp_mod
            mcp_mod._mcp_delay_cache = (0.0, 0.0, 0, [])

            from tools.mcp_tool import _get_mcp_delay_config
            delay_ms, patterns = _get_mcp_delay_config()
            assert delay_ms == 500

    def test_delay_config_reads_custom_patterns(self):
        """Custom patterns from config override defaults."""
        with patch("tools.mcp_tool.load_config", return_value={
            "agent": {
                "tool_call_delay_ms": 1000,
                "mcp_delay_tool_patterns": ["*web*", "*crawl*"]
            }
        }):
            import tools.mcp_tool as mcp_mod
            mcp_mod._mcp_delay_cache = (0.0, 0.0, 0, [])

            from tools.mcp_tool import _get_mcp_delay_config
            delay_ms, patterns = _get_mcp_delay_config()
            assert delay_ms == 1000
            assert patterns == ["*web*", "*crawl*"]

    def test_delay_config_caching(self):
        """Config is cached and not re-read within TTL."""
        mock_load = MagicMock(return_value={"agent": {"tool_call_delay_ms": 200}})
        with patch("tools.mcp_tool.load_config", mock_load):
            import tools.mcp_tool as mcp_mod
            mcp_mod._mcp_delay_cache = (0.0, 0.0, 0, [])

            from tools.mcp_tool import _get_mcp_delay_config
            _get_mcp_delay_config()
            _get_mcp_delay_config()
            _get_mcp_delay_config()

            # Should only be called once due to caching
            mock_load.assert_called_once()

    def test_delay_config_cache_expiry(self):
        """Config is re-read after TTL expires."""
        import tools.mcp_tool as mcp_mod
        original_ttl = mcp_mod._MCP_DELAY_CACHE_TTL

        try:
            mock_load = MagicMock(return_value={"agent": {"tool_call_delay_ms": 300}})
            with patch("tools.mcp_tool.load_config", mock_load):
                mcp_mod._mcp_delay_cache = (0.0, 0.0, 0, [])
                mcp_mod._MCP_DELAY_CACHE_TTL = 0.001  # 1ms TTL

                from tools.mcp_tool import _get_mcp_delay_config
                _get_mcp_delay_config()
                import time
                time.sleep(0.01)  # Wait past TTL
                _get_mcp_delay_config()

                # Should be called twice (once before expiry, once after)
                assert mock_load.call_count == 2
        finally:
            mcp_mod._MCP_DELAY_CACHE_TTL = original_ttl

    def test_delay_config_graceful_on_load_error(self):
        """If load_config raises, delay defaults to 0 with no crash."""
        with patch("tools.mcp_tool.load_config", side_effect=Exception("config broken")):
            import tools.mcp_tool as mcp_mod
            mcp_mod._mcp_delay_cache = (0.0, 0.0, 0, [])

            from tools.mcp_tool import _get_mcp_delay_config
            delay_ms, patterns = _get_mcp_delay_config()
            assert delay_ms == 0
            assert patterns == ["*search*", "*fetch*"]


class TestShouldDelayTool:
    """Verify _should_delay_tool checks patterns correctly."""

    def test_search_tool_should_delay(self):
        with patch("tools.mcp_tool._get_mcp_delay_config", return_value=(1000, ["*search*"])):
            from tools.mcp_tool import _should_delay_tool
            assert _should_delay_tool("search") is True

    def test_non_matching_tool_should_not_delay(self):
        with patch("tools.mcp_tool._get_mcp_delay_config", return_value=(1000, ["*search*"])):
            from tools.mcp_tool import _should_delay_tool
            assert _should_delay_tool("read_file") is False


class TestMcpToolHandlerDelayIntegration:
    """Verify the delay is applied in _make_tool_handler when conditions are met."""

    def test_handler_applies_delay_for_matching_tool(self):
        """When delay_ms > 0 and tool matches pattern, time.sleep is called."""
        with patch("tools.mcp_tool._get_mcp_delay_config", return_value=(500, ["*search*"])):
            with patch("time.sleep") as mock_sleep:
                with patch("tools.mcp_tool._server_error_counts", {}):
                    with patch("tools.mcp_tool._servers", {}):
                        with patch("tools.mcp_tool._CIRCUIT_BREAKER_THRESHOLD", 10):
                            from tools.mcp_tool import _make_tool_handler
                            handler = _make_tool_handler("open-websearch", "search", 300)
                            handler({})  # Call the handler

                            # Should have slept before hitting "server not connected"
                            mock_sleep.assert_called_once_with(0.5)

    def test_handler_skips_delay_for_non_matching_tool(self):
        """When tool doesn't match pattern, no sleep occurs."""
        with patch("tools.mcp_tool._get_mcp_delay_config", return_value=(500, ["*search*"])):
            with patch("time.sleep") as mock_sleep:
                with patch("tools.mcp_tool._server_error_counts", {}):
                    with patch("tools.mcp_tool._servers", {}):
                        with patch("tools.mcp_tool._CIRCUIT_BREAKER_THRESHOLD", 10):
                            from tools.mcp_tool import _make_tool_handler
                            handler = _make_tool_handler("filesystem", "read_file", 300)
                            handler({})

                            # Should NOT have slept
                            mock_sleep.assert_not_called()

    def test_handler_skips_delay_when_delay_is_zero(self):
        """When delay_ms is 0, no sleep regardless of tool name."""
        with patch("tools.mcp_tool._get_mcp_delay_config", return_value=(0, ["*search*"])):
            with patch("time.sleep") as mock_sleep:
                with patch("tools.mcp_tool._server_error_counts", {}):
                    with patch("tools.mcp_tool._servers", {}):
                        with patch("tools.mcp_tool._CIRCUIT_BREAKER_THRESHOLD", 10):
                            from tools.mcp_tool import _make_tool_handler
                            handler = _make_tool_handler("open-websearch", "search", 300)
                            handler({})

                            mock_sleep.assert_not_called()
