# Writing tasks

A task is one YAML file in `<project>/.hub/tasks/`. The file name doesn't matter. `name` does.

```yaml
name: resize-images            # required: lowercase letters, digits, - and _
description: >                 # required: what it does, when to use it, is it safe?
  Resize every image in a folder to a max width. Writes to <folder>/resized,
  never touches the originals. Safe to run anytime.
command: python3 scripts/resize.py {folder} --width {width}   # required
cwd: .                         # working dir, relative to the project root (default ".")
args:                          # optional; each {name} in command is filled from here
  folder:
    description: folder with the images
    required: true
  width:
    description: max width in pixels
    default: "1600"
  format:
    description: output format
    default: "jpg"
    choices: ["jpg", "png", "webp"]
confirm: false                 # true → phone asks "Run resize-images?" first
notify: always                 # always | failure | never   (message when it ends)
timeout: 0                     # seconds; the job is killed after this (0 = no limit)
schedule: every 6h             # "every 30m" / "every 6h" / "daily 07:30"; omit = manual
tags: [images]                 # used by /find
examples:                      # shown to agents and in TASKS.md
  - ahub run resize-images folder=~/Pictures/trip width=1200
```

## Rules

- **The command runs without a shell.** Each `{arg}` becomes one argument, so values with spaces or `;` are safe. For pipes or `cd`, call `bash -c '...' _ {arg}` (see `examples/.hub/tasks/git-status.yaml`).
- An argument left empty (no value, no default) is dropped from the command.
- **YAML gotchas:** quote `"~"`, `"yes"`, `"no"`, `"on"`, `"off"` and numbers. A bare `~` means *empty*.
- Scripts get these environment variables: `AGENT_HUB_JOB_ID` (enables `progress()`), `PYTHONUNBUFFERED=1` (live logs), and `PYTHONPATH` (so `import agenthub.client` works).
- Output (stdout and stderr) goes to `~/.config/agent-hub/logs/<job>.log`. Print useful lines: the last 15 are sent to your phone.
- Exit code 0 = ✅ done; anything else = ❌ failed.

## Check your work

```bash
ahub tasks --check      # lists tasks, shows YAML errors, exits 1 if any are broken
ahub run resize-images folder=/tmp/x --wait
ahub reload             # make the running hub (and the phone) see changes
ahub docs               # refresh .hub/TASKS.md for agents
```

## Good descriptions for agents

Agents choose tasks from the `description`, so write it for a newcomer:
1. What it does, in one sentence.
2. Side effects: what it writes, deletes or sends.
3. Whether it's safe to run anytime.

Bad: `Backup script`. Good: `Copy new photos from D:/Camera to the NAS; skips existing files; safe to run anytime.`
