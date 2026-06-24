# vLLM Timeout Recovery Pattern

## Overview

When a subagent's vLLM server (typically on port 5678) times out mid-generation, the server remains alive but loses its session context. The subagent is **idle, not dead** — it just needs a signal to resume.

## Symptoms

- Subagent dispatch shows `status: dispatched` but no result arrives
- Server responds to basic health checks (HTTP 200 on `/v1/models`)
- Subagent summary never completes; appears "sitting idle"
- May appear in session as `[ASYNC DELEGATION BATCH COMPLETE]` with a message about the subagent being idle

## Recovery Method

**Use HTTP POST to `/v1/chat/completions`, NOT raw text via socket.**

```bash
# Correct: HTTP POST with system message restating topic
curl -s http://192.168.1.224:5678/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "",
    "messages": [
      {"role": "system", "content": "You are a research assistant studying the global oil market as of mid-2026. You were researching oil prices, OPEC+, the Strait of Hormuz closure (Iran conflict), supply/demand dynamics when your connection timed out."},
      {"role": "user", "content": "continue\nplease restate your summary if you have completed"}
    ],
    "max_tokens": 2048,
    "temperature": 0.7
  }' | python3 -c "import sys,json; d=json.load(sys.stdin); print(d['choices'][0]['message']['content'])"

# Shorter version for quick recovery (avoids generation timeout)
curl -s --max-time 30 http://192.168.1.224:5678/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "",
    "messages": [
      {"role": "system", "content": "You are a research assistant studying the global oil market as of mid-2026."},
      {"role": "user", "content": "continue. Please restate your summary if you have completed."}
    ],
    "max_tokens": 1024,
    "temperature": 0.7
  }' | python3 -c "import sys,json; d=json.load(sys.stdin); print(d['choices'][0]['message']['content'][:2000])"
```

**Key details:**
- Include a `system` message that restates the topic — without it, the vLLM server responds with "I don't have context..." (it lost its session history)
- Use `--max-time 30` for shorter prompts to avoid timeout during generation (the heavy Qwen3.6-27B-FP8 model can take >15s to generate)
- The `model` field should be empty string `""` to use the server's default model

**Do NOT:**
- Send raw text via socket (`nc`, `python3 socket`) — vLLM expects HTTP, not raw text. This returns `HTTP/1.1 400 Bad Request` with "Invalid HTTP request received."
- Assume the user already sent a signal — check first, then act

## When to Use This Pattern

This pattern applies when:
1. A subagent was dispatched to a vLLM server (port 5678) and is idle
2. The dispatch succeeded but no result message has appeared in the session
3. The server is alive (responds to curl/health checks)
4. You need to recover the subagent's work without re-dispatching

## User Interaction Note

**Take initiative — don't assume the user has acted.** If you're waiting for a signal from the user, actually send it yourself rather than assuming they already did. This is a common pattern: users expect the agent to be proactive, not passive.

When in doubt about whether the user has sent a signal:
1. Check the session for recent messages
2. If no clear signal exists, send one yourself
3. Document what you did so the user can see the action
