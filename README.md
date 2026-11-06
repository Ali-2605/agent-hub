# agent-hub

Control your scripts and coding agents (Claude Code, opencode) from your phone through Telegram.

- 🔔 Get notified when scripts finish or fail, and when an agent finishes its turn
- ✅ Approve or deny agent permission prompts from the phone; the agent **waits for you**
- ❓ Answer agent questions (Claude's multiple-choice questions become buttons)
- 💬 Reply to a finished agent to keep it working
- 🎛 See each agent's **mode** and change it (plan, acceptEdits, pause, …)
- ▶️ Run your scripts, read their logs, stop them, search everything
- 🤖 Any agent can use your scripts through MCP, with no extra glue code

No AI runs inside the hub. It only relays and answers. The intelligence stays in the agents you talk to.

```
     📱 Telegram (Android)
           │  commands, buttons, replies        ▲ notifications, questions
   ┌───────┴────────────────────────────────────┴───────┐
   │  agent-hub  (one small Python service on your PC)  │
   │  tasks (YAML) · jobs + logs · questions · sessions │
   │  REST API on 127.0.0.1 · MCP server · CLI `ahub`   │
   └──┬───────────────┬────────────────┬────────────────┘
  Claude Code      opencode         your scripts
  (hooks)          (plugin)         (YAML manifest; optional `ask()` / `notify()`)
```

---

## 1. Setup (about 10 minutes)

```bash
cd agent-hub
python3 -m venv .venv && .venv/bin/pip install -e .
ln -sf "$PWD/.venv/bin/ahub" ~/.local/bin/ahub      # so `ahub` works everywhere
```

**Create your Telegram bot.** In Telegram, open **@BotFather**, send `/newbot` and copy the token. Then run:

```bash
ahub setup-telegram        # paste the token, then send any message to your bot
```

This saves the token and *your* chat id to `~/.config/agent-hub/.env`. The bot ignores messages from anyone else.

**Turn it on and off.** The hub only starts on the laptop, with a password:

```bash
ahub password              # once: set the start password
ahub on                    # asks for the password, starts the hub in the background
ahub off                   # stops it (no password). From the phone: /off
```

While the hub is off, agents work exactly as if agent-hub didn't exist, and they ask in the terminal. Messages you send the bot while it is off are ignored when it comes back on, so an old `/run` never fires later. Logs: `~/.config/agent-hub/hub.log`.

The phone can turn the hub **off** but never **on**. Only someone at the laptop who knows the password can start it. (`ahub install service` exists for an always-on setup without a password; it refuses to install while a password is set.)

**Connect your agents:**

```bash
ahub install claude        # hooks in ~/.claude/settings.json + MCP server (user scope)
ahub install opencode      # plugin in ~/.config/opencode/plugins + MCP server
ahub install skill         # teaches agents how to use agent-hub (Claude + opencode skills)
ahub doctor                # checks everything
```

Every installer backs up the file it changes (`*.bak-<date>`). Add `--dry-run` to see the change first.

**Add a project with scripts:**

```bash
ahub init ~/code/my-project     # creates .hub/tasks/hello.yaml and registers the project
```

Send `/help` to your bot. Done.

> **The hooks are safe to leave on.** If the hub is not running, the hooks do nothing and Claude Code and opencode behave as if agent-hub didn't exist. While you are *not* away, the hub stays silent and only records sessions.

---

## 2. Using it from the phone

| Command | What it does |
|---|---|
| `/status` | Away or at the computer, running jobs, agent sessions with mode, pending questions |
| `/away` · `/back` | **Away**: agent questions, permissions and "finished" messages come to the phone. **Back**: they go to the terminal again |
| `/pending` | Everything waiting for your answer |
| `/tasks` | Your scripts. Tap one to run it |
| `/run name key=value …` | Run a script with arguments |
| `/jobs` · `/log id` · `/stop id` | Recent runs, their output, kill one |
| `/sessions` | Claude Code and opencode sessions. Tap one to see its last message and change its mode |
| `/mode id mode` | Change a session's mode (see below) |
| 💬 in `/sessions` · `/msg id prompt` | Send a new prompt to an existing session, even one that's idle in your laptop terminal (see below) |
| `/route id auto\|phone\|local` | Per session: phone only while away (default), always phone, or never phone |
| `/claude project prompt` | Start Claude Code in a project (add `project:plan` to pick the mode) |
| `/opencode project prompt` | Start opencode in a project (add `project:plan` to pick the agent) |
| `/find text` | Plain-text search across tasks, job logs, agent messages and history |
| `/get project path` | Send you a file from a project folder (or a folder where an agent session ran), e.g. `/get myapp docs/report.pdf` |
| `/reload` | Reload task files after editing them |

**Answering:** tap a button, or *reply* to the message (swipe it right in Telegram) with text. If only one question is waiting, you can just type the answer.

- Typing on a **permission** prompt counts as a *deny*, and the agent receives your text as the reason ("no, use the staging DB instead").
- Typing on a **"finished"** message sends your text to the agent as its next prompt, so it keeps going. Tap ✅ **Done** to let it stop.
- If you **don't reply**, the agent waits `stop_wait_minutes` (default 30), then stops normally and the session goes idle. Sending `/back` lets waiting laptop sessions go right away. You can still reach an idle session later with 💬 in `/sessions`.

### Messaging a session that's already open

In `/sessions`, tap a session, then **💬 Message this session** and reply with your prompt (or type `/msg <id> <prompt>`). The hub continues that conversation in the background, in the session's folder, with all its earlier context:

- **Claude Code:** `claude -p <prompt> --resume <id> --fork-session`. The fork gets its own id, so the window still open on the laptop isn't disturbed. To pick the conversation up at the laptop later, run `claude --resume` and choose the forked session.
- **opencode:** `opencode run -s <id>` continues the same session.
- Permission prompts and questions come to the phone. When it finishes, you get the answer with "reply to continue".
- The answer arrives on the phone, not in the open terminal window.
- Agent text goes through Telegram. For sensitive data, ask the agent to save results to a file and just send you the path.

### Agent modes

Each session shows its mode. Modes use Claude Code's names, so they mean the same thing on the phone as in the terminal:

| Mode | What the hub does with tool calls |
|---|---|
| 🛡 `default` | Asks you on the phone before anything that changes things (reading is fine) |
| ✏️ `acceptEdits` | File edits are allowed. Shell commands and other actions ask you |
| 📝 `plan` | Edits are refused. Other actions ask you. The agent is told it's in plan mode |
| 🤖 `auto` | The agent's own rules decide. You're only asked when it would ask |
| ⚡ `bypassPermissions` | Everything is allowed |
| ⏸ `pause` | Every tool call **waits** until you pick another mode |
| ↩️ `clear` | Stop overriding. The agent's own mode applies again |

How this works under the hood:
- **Claude Code** can't have its mode changed from outside at an arbitrary moment. So the hub *enforces* your chosen mode on every tool call right away. At Claude's next permission prompt, it also switches Claude's real mode to match.
- **opencode**: the plugin switches the session's agent (`plan` → plan agent, the other modes → build) and enforces the same rules.

If Claude Code is in `auto` mode and still asks for permission or asks a question while you're away, the hub sends it to your phone and **Claude waits until you answer**. That is the default: `request_timeout_minutes: 0` means wait forever.

---

## 3. Your scripts

There are three levels, and you can use any of them.

### Level 1: no code changes. Write a manifest.

`<project>/.hub/tasks/backup.yaml`:

```yaml
name: backup
description: Copy new photos to the NAS. Safe to run anytime; skips files already copied.
command: python3 scripts/backup.py --src {src} --dry-run {dry_run}
args:
  src:
    description: folder with the photos
    default: "/mnt/d/Camera"
  dry_run:
    description: only show what would be copied
    default: "no"
    choices: ["yes", "no"]
notify: always          # always | failure | never
confirm: false          # true = ask on the phone before every run
timeout: 3600           # seconds (0 = none)
schedule: daily 03:00   # or "every 30m"; omit for manual only
tags: [backup]
examples:
  - ahub run backup src=/mnt/e/Phone
```

The script's output is saved, and you get a message with the last lines when it finishes. Full reference: [docs/writing-tasks.md](docs/writing-tasks.md).

### Level 2: talk to the phone from inside a Python script

```python
from agenthub.client import notify, ask, confirm, progress, send_file

progress(30, "copied 300/1000")                           # shows in /jobs, no buzz
if not confirm("Found 12 GB of duplicates. Delete them?"):   # buttons Yes / No
    raise SystemExit
name = ask("What should the archive be called?")          # free-text reply
notify("Backup finished ✅")
send_file("out/report.pdf", "Weekly report")              # arrives as a document
```

Scripts started by the hub can always import this, even with a system `python3`. Elsewhere, install the repo (`pip install -e`) in that environment. `agenthub/client.py` uses only the standard library. If the hub is down, `notify` and `progress` do nothing, and `ask` raises `HubUnavailable`.

### Level 3: any language, from the shell

```bash
ahub notify "deploy done" --title myapp
answer=$(ahub ask "Deploy to prod?" -o Yes -o No) && [ "$answer" = Yes ] && ./deploy.sh
ahub progress 50 "halfway"
ahub send out/report.pdf -c "Weekly report"   # one or more files, 50 MB each
ahub run backup src=/mnt/e --wait     # run a task and print its output
```

---

## 4. Making agents understand your tools

The task manifest is the single source of truth. Agents get it in three ways, and all of them are generated automatically:

1. **MCP server** (`ahub install claude` / `opencode` registers it). Tools: `list_tasks`, `run_task`, `job_log`, `list_jobs`, `notify_user`, `send_file`, `ask_user`, `hub_status`. Any MCP client (Cursor, etc.) can use it with `command: ahub mcp`.
2. **Skill** (`ahub install skill`). Claude Code and opencode learn *when* to use the hub, for example asking you on the phone when you're away.
3. **`TASKS.md`** (`ahub docs` writes `<project>/.hub/TASKS.md`). A readable catalog for agents without MCP. Point to it from the project's `AGENTS.md` / `CLAUDE.md`:

```markdown
## Scripts
This project's scripts are registered with agent-hub. See .hub/TASKS.md.
Run them with `ahub run <name> key=value --wait`, or the agent-hub MCP tools.
```

**Writing tasks agents pick up fast:** one-sentence `description` that says what it does *and* whether it's safe; a description on every argument; one real example. Mark anything destructive with `confirm: true`.

---

## 5. Using it in another project

```bash
ahub init ~/code/other-project            # adds .hub/tasks/ + registers it
$EDITOR ~/code/other-project/.hub/tasks/hello.yaml
ahub reload                                # or /reload on the phone
ahub docs                                  # refresh TASKS.md for agents
```

Projects are just a name and a folder in `~/.config/agent-hub/config.yaml`:

```yaml
projects:
  photos: ~/code/photo-tools
  site: ~/code/my-site
```

See [config.example.yaml](config.example.yaml) for all settings (timeouts, what happens on timeout, default Claude mode for phone runs).

---

## 6. Security

- The hub listens on **127.0.0.1 only**, and every request needs the token in `~/.config/agent-hub/token`.
- Telegram uses outgoing long polling, so there are **no open ports** and no webhooks.
- The bot only obeys **your** chat id. Everyone else is ignored and logged.
- Scripts run without a shell, and arguments are passed as single values, so `/run x path="; rm -rf /"` can't inject commands.
- There is **no "run any shell command" feature from the phone**. Only tasks you wrote can run. That's deliberate.
- Anyone holding your unlocked phone with Telegram open can control your agents. Lock your phone. Access belongs to your **Telegram account**, so it also works from Telegram Desktop/Web: turn on Two-Step Verification in Telegram.
- **Sending files** (`ahub send`, `send_file`, `/get`) refuses the hub's own folder, `~/.ssh`, `~/.gnupg`, `~/.aws`, `.env*`, `*.pem` and `*.key`, and anything over Telegram's 50 MB bot limit. `/get` from the phone only reaches files inside registered projects or folders where an agent session ran, never your whole disk. Files pass through Telegram's servers, so think twice about sensitive data.
- **The hub only starts with your password, on the laptop** (`ahub on`). Only a salted PBKDF2 hash is stored (`~/.config/agent-hub/password`). The phone can turn it off (`/off`) but not on. The password stops accidental or automatic starts; it can't stop someone who already controls your laptop account.

---

## 7. Troubleshooting

| Problem | Fix |
|---|---|
| Bot doesn't answer | Is it on? `ahub on`. Then `ahub doctor` and `~/.config/agent-hub/hub.log` |
| No questions on the phone | Did you send `/away`? Check the session's route with `/sessions` |
| Claude asks in the terminal anyway | You're not away, or the session's route is `local`. If the hub restarted mid-question, the hook falls back to the terminal |
| opencode plugin does nothing | opencode runs a **background server** that loads plugins. Check `~/.local/share/opencode/log/opencode.log` for `failed to load plugin` / `failed to reload plugins`. One broken plugin (e.g. a missing file in another npm plugin's cache) can stop *all* plugins from loading |
| Task missing from `/tasks` | `ahub tasks --check` shows YAML errors. Quote `~`, `yes`/`no` and numbers in YAML (`default: "~"`); a bare `~` means *empty* |
| WSL: hub stops when you close the terminal | `ahub on` detaches the hub from the terminal, but WSL itself may shut down when idle: keep one terminal open, or set `vmIdleTimeout` in `%UserProfile%\.wslconfig` (check your WSL version's docs) |
| Windows went to sleep | Everything pauses. Set Windows power settings to not sleep while plugged in |

---

## 8. What's verified and what's best-effort

Tested end to end against **real Claude Code 2.1** (`claude -p`):
- permission prompt → phone → allow → command ran
- "finished" → phone reply → Claude continued with the reply → Done
- phone-set `plan` mode blocked a `Write` with the reason shown to Claude

Tested against **real opencode 2.1**:
- plugin loads and registers sessions and the current agent
- phone-set `plan` mode blocked a write *and* switched the session to the plan agent

Best-effort (written against opencode's 2.x plugin API but not run live, because the free model quota ran out during testing):
- the "reply to continue" loop inside an opencode TUI session
- answering opencode permission prompts from the phone

Claude's multiple-choice questions (`AskUserQuestion`) are answered through the documented hook mechanism. This is unit-tested, not tested live.

**Telegram** was verified live: notifications, button questions and answers reaching the PC. The rest of the bot is covered by `tests/test_telegram.py` against a fake Telegram API.

**opencode MCP**: `ahub install opencode` registers it with opencode's own `opencode mcp add` command, which writes the 2.x format `{"mcp": {"servers": {…}}}`. The MCP server itself was tested directly (it listed tasks and ran one). opencode connecting to it was not tested.

---

## 9. Repository layout

```
agenthub/
  hub.py          all behaviour: requests, sessions, modes, jobs, scheduler, search
  policy.py       pure "should this go to the phone?" rules (easy to read/test)
  telegram.py     the bot: one cmd_<name> method per command
  server.py       REST API (open http://127.0.0.1:8765/docs while running)
  hooks.py        Claude Code hook relay (`ahub hook claude`)
  mcp_server.py   MCP tools for agents (`ahub mcp`)
  client.py       stdlib-only client for scripts: notify / ask / confirm / progress
  tasks.py        YAML task manifests → commands, catalog
  cli.py          the `ahub` command
integrations/opencode/agent-hub.js   opencode plugin
skills/agent-hub/SKILL.md            skill for agents using the hub
examples/.hub/tasks/                 example tasks (disk usage, git status, long demo, cleanup)
docs/                                writing-tasks.md, how-it-works.md
tests/                               `pytest` (end-to-end with the real hook relay)
```

Want to extend it? Read [AGENTS.md](AGENTS.md). It's written so you, or an agent, can add features without reading the whole codebase.
