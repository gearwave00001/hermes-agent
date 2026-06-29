# open-websearch Rate Limiting & ECONNRESET Troubleshooting

## Problem Pattern

When running 4+ concurrent subagents, the `open-websearch` MCP server logs frequent errors:

```
DuckDuckGo HTML search failed: AxiosError: read ECONNRESET
at ClientRequest.handleRequest (http.js:866)
at async searchDuckDuckGoHtml (searchDuckDuckGo.js:194)
```

## Root Cause

The `searchService.ts` uses `Promise.all(tasks)` to fire parallel engine requests. Each subagent makes multiple independent Axios HTTP connections to DuckDuckGo simultaneously. The HTML scraping path (`searchDuckDuckGoHtml`) is connection-heavy — more concurrent connections than DuckDuckGo handles gracefully, leading to resets mid-flight.

## Configuration Reference

### Environment Variables (from config.ts)

| Variable | Default | Effect on Rate Limiting |
|----------|---------|------------------------|
| `SEARCH_MODE` | `auto` | `request` = lighter HTTP-only; `auto` = request + Playwright fallback; `playwright` = force browser |
| `DEFAULT_SEARCH_ENGINE` | `bing` | Set to `duckduckgo` if that's your primary engine |
| `ALLOWED_SEARCH_ENGINES` | (all) | Comma-separated list limits which engines are used |
| `USE_PROXY` | `false` | Enable HTTP proxy; affects connection pooling |

### Hermes Config (config.yaml)

```yaml
open-websearch:
  url: http://<host>:3100/mcp
  timeout: 120
  connect_timeout: 30
  engines: '["duckduckgo"]'   # restrict to one engine reduces parallelism pressure
  env:
    SEARCH_MODE: request        # KEY FIX for ECONNRESET under load
```

## Fix Priority

1. **`SEARCH_MODE=request`** — Switches from heavy HTML scraping to lighter HTTP-only path. Most impactful fix.
2. **Restrict `engines`** — Already set in most configs; ensures only one engine is queried per search call.
3. **Reduce `limit`** — Fewer results per search means fewer concurrent connections. Default is 10; try 5 under heavy load.

## Verification

After applying `SEARCH_MODE=request`:
- Check open-websearch logs for reduced ECONNRESET frequency
- Subagent API call counts should be similar, but with fewer transient failures
- Search latency may decrease slightly due to lighter HTTP path

## Notes

- Errors are transient — the search service retries automatically
- The Playwright fallback (in `auto` mode) adds browser overhead that compounds under concurrent load
- When Playwright is unavailable, `fetchWebContent` stays on the request-only path regardless of mode
- This is NOT a permanent rate limit from DuckDuckGo; it's connection exhaustion from bursty parallel requests
