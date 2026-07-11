# MCP Server Discovery via curl

When you need to inspect an MCP server's tools before configuring it in Hermes, use these curl commands. Works with any HTTP/StreamableHTTP MCP server.

## Step 1: Initialize Session

```bash
curl -s -X POST http://SERVER_URL/mcp \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -D /tmp/headers.txt \
  -o /tmp/body.txt \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"discover","version":"1.0"}}}'
```

Key points:
- `-D /tmp/headers.txt` captures response headers (including `Mcp-Session-Id`)
- `-o /tmp/body.txt` captures the JSON response body
- Server must accept BOTH content types in Accept header

## Step 2: Extract Session ID

```bash
grep -oP '(?<=Mcp-Session-Id: )[^\r\n]+' /tmp/headers.txt
```

The session ID is a UUID in the HTTP response header, NOT in the JSON body. This is the most common mistake — searching the body for it won't work.

## Step 3: List Tools

```bash
curl -s -X POST http://SERVER_URL/mcp \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -H "Mcp-Session-Id: <session-id-from-step-2>" \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}' | grep '^data:' | sed 's/^data: //'
```

## Step 4: Test a Tool Call

```bash
curl -s -X POST http://SERVER_URL/mcp \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -H "Mcp-Session-Id: <session-id>" \
  -d '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"TOOL_NAME","arguments":{"arg1":"value"}}}' | grep '^data:' | sed 's/^data: //'
```

## Real Example: open-websearch Server

Server URL: `http://192.168.1.225:3100/mcp`

Tools discovered:
- `search` — web search (DuckDuckGo, Bing) with query/limit/searchMode/engines params
- `fetchWebContent` — fetch any public URL with maxChars/readability/includeLinks options
- `fetchGithubReadme` — fetch README from a GitHub repo URL
- `fetchLinuxDoArticle` — fetch linux.do post content
- `fetchCsdnArticle` — fetch csdn post content
- `fetchJuejinArticle` — fetch juejin post content

## Troubleshooting Discovery

| Symptom | Cause | Fix |
|---------|-------|-----|
| "Not Acceptable: Client must accept both application/json and text/event-stream" | Missing Accept header or incomplete value | Add `-H "Accept: application/json, text/event-stream"` |
| Session ID is empty in step 2 | Headers not captured separately | Use `-D` flag to dump headers to file |
| No `data:` lines in response | Tool returned error or server issue | Check body.txt for JSONRPC error response |
| "Invalid or missing session ID" | Session expired or wrong session passed | Re-initialize and get fresh session ID |
