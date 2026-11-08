# AGENTS.md — working on agent-hub

Read this before changing code. (Using the hub rather than changing it? See `skills/agent-hub/SKILL.md`.)

## Mental model (30 seconds)

- `Hub` (agenthub/hub.py) owns **all** state and behaviour. Everything else is a thin adapter:
  - `server.py`: REST routes → Hub methods
  - `telegram.py`: phone commands and buttons → Hub methods; Hub → `notifier.send()`
  - `hooks.py` / `integrations/opencode/agent-hub.js`: forward agent events to `/api/hook/*`
  - `mcp_server.py`, `cli.py`, `client.py`: call the REST API
- **Requests** are anything waiting for the user (question, approval, "reply to continue"). They have `options` (buttons) and `allow_text`. `Hub.ask()` = create a request and wait for it.
- **Hook tickets.** When a hook needs the user, the Hub returns `{"pending": ticket}` and the hook polls `/api/hook/wait/<ticket>` until it gets `{"output": <hook JSON>}`. The decision logic lives in the Hub and stays in Python; the hook stays a dumb relay.
- **Fail open.** If the hub is unreachable, hooks and the plugin print nothing and exit 0, so the agent runs normally. Never break this.
- `policy.py` is pure (no I/O): routing (`is_routed`) and the mode table (`tool_decision`).
- **Phone lock.** The phone starts locked. `TelegramBot._on_message` / `_on_button` refuse anything not in `OPEN_COMMANDS` until `/unlock <PIN>` (`lock.check_pin`). New `cmd_*` methods are locked automatically. Add one to `OPEN_COMMANDS` only if it is read-only. Never add a way to set the PIN or allow bypass from the phone, and never log the PIN or the bot token (use `_redact`).
- **Power.** The hub only starts after the password check (`lock.py`, `cli._require_password`), from `ahub on` or `ahub serve`. `Hub.turn_off()` (phone `/off`, `ahub off` → `POST /api/shutdown`) stops it. Never add a way to start it from the phone.

## Common changes

| I want to… | Do this |
|---|---|
| Add a phone command | `async def cmd_<name>(self, args)` in `telegram.py` + a line in `HELP`. Dispatch is automatic, and it is locked until `/unlock` |
| Add a button | Add a `(label, "prefix:data")` tuple and handle `prefix` in `TelegramBot._on_button`. callback_data ≤ 64 bytes |
| Change what goes to the phone | `policy.py` (+ `tests/test_policy.py`) |
| React to a new Claude hook event | `Hub.handle_claude_hook`; register the event in `cli.claude_hooks_config` |
| Add an MCP tool | `@mcp.tool() @_safe def name(...) -> str` in `mcp_server.py`, with a docstring (agents read it) |
| Add a task field | `tasks.py` (`Task`, `parse_task`, `catalog_markdown`) + `docs/writing-tasks.md` |
| New notification channel | Implement `send(text, buttons)` / `edit(id, text, buttons)` (see `Notifier` in hub.py) |

## Rules

- Python 3.10+. `client.py`, `config.py`, `hooks.py` must stay **stdlib-only** (they run on every tool call and inside users' scripts).
- Messages to Telegram are HTML: escape user/agent text with `esc()`.
- No shell for tasks: build argv with `Task.build_argv`.
- Keep the hub non-intelligent: no LLM calls.
- Update README/docs when behaviour changes.

## Test

```bash
.venv/bin/python -m pytest -q        # unit + end-to-end (real hub, real `ahub hook claude`, fake phone)
```

`tests/test_flow.py` shows how to drive every flow without Telegram: answer requests with `POST /api/requests/<id>/answer`.

## Facts about the agents (verified Nov 2026)

- Claude Code hook input has `permission_mode` (`default|plan|acceptEdits|auto|dontAsk|bypassPermissions`). `PermissionRequest` output can include `updatedPermissions: [{"type":"setMode","mode":…,"destination":"session"}]`, and that is the only way to change Claude's real mode from outside. `AskUserQuestion` is answered by a PreToolUse `allow` with `updatedInput` = questions + `answers`. `Stop` → `{"decision":"block","reason":…}` makes Claude continue.
- opencode 2.x plugins: `export default { id, setup(ctx) }`. They use `ctx.tool.hook("execute.before", a => …)` (throw to block; `a.tool, a.sessionID, a.agent, a.input`), `ctx.permission.hook("evaluate", h => …)` (set `h.effect`), `ctx.event.subscribe()`, `ctx.session.prompt({sessionID, text})` and `ctx.session.switchAgent({sessionID, agent})`. Plugins run inside opencode's **background service**, which doesn't inherit your shell env. `opencode run --standalone` runs a private server that does.
