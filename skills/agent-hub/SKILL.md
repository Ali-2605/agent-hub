---
name: agent-hub
description: Use the user's agent-hub to run their registered scripts (tasks), read job logs, and reach the user on their phone (notify, ask a question and wait for the answer). Use when the user mentions their scripts/tasks/jobs, asks to be notified or pinged, says they are away or on their phone, or when you need a decision while the user may not be at the computer.
---

# agent-hub

agent-hub runs on the user's computer. It knows the user's scripts ("tasks") and is connected to the user's phone through Telegram.

## Tools

Prefer the MCP tools (server `agent-hub`). Use the CLI if MCP isn't available.

| Need | MCP tool | CLI |
|---|---|---|
| See what scripts exist | `list_tasks` | `ahub tasks` (or read `.hub/TASKS.md`) |
| Run one and get its output | `run_task(task, args, wait=true)` | `ahub run <task> key=value --wait` |
| Check a job | `job_log(job_id)` / `list_jobs` | `ahub log <id>` / `ahub jobs` |
| Tell the user something | `notify_user(text)` | `ahub notify "text"` |
| Send a file (document, report, chart) | `send_file(path, caption)` | `ahub send <file> -c "caption"` |
| Ask the user and wait | `ask_user(question, options)` | `ahub ask "question" -o A -o B` |
| Is the user away? | `hub_status` (`user_away`) | `ahub status` |

## Rules

- Prefer a registered task over writing your own command for the same job. Tasks are the user's vetted way of doing things.
- Pass arguments exactly as the task describes them (strings, `key=value`). Don't invent arguments.
- Tasks marked "asks the user" wait for approval on the phone. That's expected, so don't retry them.
- `notify_user` is for things the user would want on their phone: a long job finished, you're blocked, something failed. Don't send chatty progress updates.
- When the user asks for a document/report/export while away, save it to a file in the project and send it with `send_file`. Don't paste long content into messages. Secrets (.env, keys) are refused.
- Use `ask_user` with short options (2–4 buttons) when the decision has clear choices. The user is often outside and on their phone.
- Permission prompts and your normal questions already reach the phone automatically when the user is away. You don't need `ask_user` for those.

## Adding a task for the user

Create `<project>/.hub/tasks/<name>.yaml`:

```yaml
name: my-task
description: One sentence — what it does and whether it is safe to run anytime.
command: python3 scripts/my_task.py --target {target}
args:
  target: {description: what to act on, required: true}
confirm: false   # true if destructive
notify: always
```

Then run `ahub reload` and `ahub docs`. Quote YAML values like `"~"`, `"yes"`, `"10"`.
