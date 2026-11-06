# How it works

## The pieces

| Piece | Runs where | Talks to |
|---|---|---|
| Hub (`ahub serve`) | systemd user service, 127.0.0.1:8765 | Telegram (outgoing long polling), SQLite, your scripts |
| Claude Code hook (`ahub hook claude`) | started by Claude Code on every event | Hub REST |
| opencode plugin (`agent-hub.js`) | inside opencode's server | Hub REST |
| MCP server (`ahub mcp`) | started by the agent | Hub REST |
| Your scripts | started by the hub as jobs | Hub REST via `agenthub.client` (optional) |

State lives in `~/.config/agent-hub/`: `hub.db` (SQLite), `logs/`, `config.yaml`, `.env`, `token`.

## Routing: when does the phone get involved?

```
           routing = local ─────────────────────────► terminal (hub only records)
session ─► routing = phone ─────────────────────────► phone
           routing = auto (default) ─► /away ON  ───► phone
                                    └► /away OFF ───► terminal
```

- Agent runs started from the phone (`/claude`, `/opencode`) always route to the phone.
- A mode you set with `/mode` always applies, and its approvals come to the phone.

## Claude Code flows

| Claude event | When routed to the phone |
|---|---|
| `PermissionRequest` (Claude would ask you) | 🔐 message with Allow / Always allow / Deny. Claude waits. Typed text = deny + reason |
| `PreToolUse` `AskUserQuestion` | one ❓ message per question, options as buttons, typed answers allowed. Answers go back via `updatedInput.answers` |
| `PreToolUse` (any tool) with a `/mode` override | mode table in `policy.py`: pass, allow, deny, ask the phone, or hold (pause) |
| `Stop` (Claude finished its turn) | 💬 last message + ✅ Done. A typed reply → `{"decision":"block","reason": <your text>}`, so Claude keeps working. No reply within `stop_wait_minutes` → Claude stops normally |
| `Notification` | forwarded (except permission/idle ones, which are already handled) |
| `SessionStart/End`, `UserPromptSubmit` | recorded for `/sessions` |

Changing Claude's real mode: the hub enforces your mode itself on every tool call. At the next `PermissionRequest` it also returns `setMode` so Claude's own mode follows.

## opencode flows (2.x plugin API)

| Hook | Behaviour |
|---|---|
| `tool.hook("execute.before")` | the same mode table. Deny = throw (opencode shows the reason) |
| `permission.hook("evaluate")` when the effect is `ask` | asks the phone, sets the effect to allow or deny |
| event `session.idle` | 💬 last text + Done. A reply → `session.prompt` |
| `/mode` | the plugin calls `session.switchAgent` (plan ↔ build) |

Phone-started opencode runs use `opencode run --standalone -s <id>`. The run exits when finished, so "reply to continue" starts a new run in the same session.

## Waiting without timeouts

Hooks poll `/api/hook/wait/<ticket>` every 50 s and scripts poll `/api/requests/<id>/wait`, so nothing depends on long HTTP connections. Claude hook timeouts are set to 86400 s (24 h). If the hub restarts while a hook waits, the ticket is gone: the hook gets a 404 and falls back to the terminal.
