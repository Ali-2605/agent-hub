"""MCP server:  `ahub mcp`  (stdio). Gives ANY MCP-capable agent (Claude Code,
opencode, Cursor, ...) the same powers you have on the phone.

Tools are thin wrappers over the hub's REST API, so they always reflect the
current task files without regenerating anything.
"""
from __future__ import annotations

import functools
import json
import os

from mcp.server.mcpserver import MCPServer

from .client import HubUnavailable, call, wait_for

INSTRUCTIONS = """agent-hub runs the user's scripts ("tasks") on their computer and connects
to their phone (Telegram). Use list_tasks to discover scripts (each has a description,
arguments and examples), run_task to run one, and job_log to read its output.
Use notify_user for important updates while the user is away, and ask_user when you
need a decision and the user may not be at the computer (it waits for their answer)."""

mcp = MCPServer("agent-hub", instructions=INSTRUCTIONS)


def _safe(fn):
    @functools.wraps(fn)  # keeps the signature so MCP can build the tool schema
    def wrapper(*a, **kw):
        try:
            return fn(*a, **kw)
        except HubUnavailable as e:
            return f"ERROR: {e}. The user turns it on with `ahub on` on the laptop; until then just work normally."
        except RuntimeError as e:
            return f"ERROR: {e}"
    return wrapper


@mcp.tool()
@_safe
def list_tasks() -> str:
    """List the user's runnable scripts with descriptions, arguments and examples."""
    tasks = call("GET", "/api/tasks")
    if not tasks:
        return "No tasks are registered yet."
    out = []
    for t in tasks:
        out.append(f"- {t['id']}: {t['description']}\n  usage: {t['usage']}")
        for a in t["args"]:
            out.append(f"    {a['name']}: {a['description']}"
                       + (f" (default {a['default']})" if a["default"] is not None else "")
                       + (" (required)" if a["required"] else "")
                       + (f" choices={a['choices']}" if a["choices"] else ""))
        if t["confirm"]:
            out.append("  (the user must approve on their phone before it runs)")
    return "\n".join(out)


@mcp.tool()
@_safe
def run_task(task: str, args: dict[str, str] | None = None, wait: bool = True, wait_seconds: int = 300) -> str:
    """Run a task by id ("project/name" or "name"). args are key=value strings.
    With wait=true, waits up to wait_seconds and returns the exit status and output tail."""
    import time
    job = call("POST", "/api/run", {"task": task, "args": args or {}})
    if not wait:
        return f"started job {job['id']} (status {job['status']}). Use job_log('{job['id']}') later."
    deadline = time.time() + wait_seconds
    while time.time() < deadline:
        job = call("GET", f"/api/jobs/{job['id']}")
        if job["status"] not in ("running", "starting", "waiting"):
            break
        time.sleep(2)
    log = call("GET", f"/api/jobs/{job['id']}/log?lines=60")["log"]
    return f"job {job['id']} status={job['status']} exit={job['exit_code']}\n--- output (tail) ---\n{log}"


@mcp.tool()
@_safe
def job_log(job_id: str, lines: int = 100) -> str:
    """Show status and the last lines of output of a job."""
    r = call("GET", f"/api/jobs/{job_id}/log?lines={lines}")
    j = r["job"]
    return f"job {j['id']} task={j['task']} status={j['status']} exit={j['exit_code']}\n{r['log']}"


@mcp.tool()
@_safe
def list_jobs(limit: int = 10) -> str:
    """Recent jobs (runs of tasks and agents) with their status."""
    jobs = call("GET", f"/api/jobs?limit={limit}")
    return "\n".join(f"{j['id']} {j['status']:<9} {j['task']}" for j in jobs) or "no jobs"


@mcp.tool()
@_safe
def notify_user(text: str) -> str:
    """Send a short message to the user's phone. Use for important milestones or problems."""
    call("POST", "/api/notify", {"text": text, "title": os.path.basename(os.getcwd())})
    return "sent"


@mcp.tool()
@_safe
def ask_user(question: str, options: list[str] | None = None, timeout_minutes: int = 0) -> str:
    """Ask the user on their phone and WAIT for the answer. Give options for buttons,
    or none for a free-text answer. timeout_minutes=0 waits indefinitely."""
    req = call("POST", "/api/requests", {"kind": "question", "title": question, "options": options or [],
                                         "allow_text": True, "source": "mcp"})
    done = wait_for(req["id"], timeout_minutes * 60 if timeout_minutes else None)
    if done["status"] != "answered":
        return f"(no answer: {done['status']})"
    return done["answer"]


@mcp.tool()
@_safe
def hub_status() -> str:
    """Overview: is the user away, running jobs, agent sessions, pending questions."""
    st = call("GET", "/api/status")
    return json.dumps({
        "user_away": st["away"],
        "running_jobs": [{"id": j["id"], "task": j["task"], "progress": j["progress"]} for j in st["running_jobs"]],
        "sessions": [{"id": s["id"][:8], "agent": s["agent"], "cwd": s["cwd"],
                      "mode": s["override_mode"] or s["agent_mode"]} for s in st["sessions"]],
        "pending_questions": len(st["pending"]),
    }, indent=1)


def main() -> None:
    mcp.run("stdio")
