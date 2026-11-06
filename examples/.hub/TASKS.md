# Task catalog

Run with `ahub run <task> key=value ...` or the MCP tool `run_task`. Generated from .hub/tasks/*.yaml — edit those files, not this one.

## examples/clean-tmp

Delete files older than N days from a scratch folder. DESTRUCTIVE, so it asks for confirmation on the phone before running.

- usage: `ahub run clean-tmp dir=<dir> days=7`
  - `dir`: folder to clean (required)
  - `days`: delete files older than this many days (default `7`)
- asks the user on the phone before running

## examples/disk-usage

Show free disk space and the biggest folders in a directory. Read-only, safe to run anytime.

- usage: `ahub run disk-usage path=~ top=10`
  - `path`: folder to inspect (default `~`)
  - `top`: how many folders to list (default `10`)
- example: `ahub run disk-usage path=/mnt/c/Users top=5`

## examples/git-status

Show branch, uncommitted changes and recent commits of a git repository. Read-only.

- usage: `ahub run git-status repo=<repo>`
  - `repo`: path to the repository (required)
- example: `ahub run git-status repo=~/code/projects/Code_Agent/agent-hub`

## examples/long-demo

Demo of a long-running script that reports progress and asks the user a question on the phone halfway through. Harmless; use it to test the setup.

- usage: `ahub run long-demo steps=10`
  - `steps`: number of 1-second steps (default `10`)
