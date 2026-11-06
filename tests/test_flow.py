"""End-to-end: a real hub on a random port, a fake phone (ConsoleNotifier),
and the real `ahub hook claude` relay talking to it."""
import json
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"
AHUB = str(Path(sys.executable).parent / "ahub")


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture(scope="module")
def hub(tmp_path_factory):
    home = tmp_path_factory.mktemp("hubhome")
    port = free_port()
    os.environ.update(AGENT_HUB_HOME=str(home), AGENT_HUB_PORT=str(port))
    (home / "config.yaml").write_text(f"projects:\n  examples: {EXAMPLES}\nstop_wait_minutes: 1\n")
    import uvicorn

    from agenthub.config import load_config
    from agenthub.hub import ConsoleNotifier, Hub
    from agenthub.server import create_app

    h = Hub(load_config(), ConsoleNotifier())
    server = uvicorn.Server(uvicorn.Config(create_app(h, with_telegram=False), host="127.0.0.1", port=port,
                                           log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    from agenthub import client
    for _ in range(50):
        if client.is_up():
            break
        time.sleep(0.1)
    yield h
    server.should_exit = True


def call(*a, **kw):
    from agenthub.client import call as c
    return c(*a, **kw)


def wait_pending(n=1, timeout=10):
    deadline = time.time() + timeout
    while time.time() < deadline:
        pending = call("GET", "/api/requests")
        if len(pending) >= n:
            return pending
        time.sleep(0.1)
    raise AssertionError("no pending request appeared")


def run_hook(payload, env_extra=None):
    env = {**os.environ, **(env_extra or {})}
    return subprocess.Popen([AHUB, "hook", "claude"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, env=env)


def finish(proc, payload):
    out, err = proc.communicate(json.dumps(payload), timeout=30)
    return json.loads(out) if out.strip() else None


BASE = {"session_id": "sess-1", "cwd": "/tmp/proj", "permission_mode": "default", "transcript_path": "/dev/null"}


def test_not_away_hook_is_silent(hub):
    call("POST", "/api/away", {"away": False})
    p = run_hook(None)
    assert finish(p, {**BASE, "hook_event_name": "PreToolUse", "tool_name": "Bash",
                      "tool_input": {"command": "ls"}}) is None


def test_permission_request_goes_to_phone_when_away(hub):
    call("POST", "/api/away", {"away": True})
    payload = {**BASE, "hook_event_name": "PermissionRequest", "tool_name": "Bash",
               "tool_input": {"command": "rm -rf build"}}
    p = run_hook(payload)
    p.stdin.write(json.dumps(payload))
    p.stdin.close()
    req = wait_pending()[0]
    assert "rm -rf build" in req["body"]
    call("POST", f"/api/requests/{req['id']}/answer", {"answer": "✅ Allow"})
    out = json.loads(p.stdout.read())
    assert out["hookSpecificOutput"]["decision"]["behavior"] == "allow"
    call("POST", "/api/away", {"away": False})


def test_mode_override_plan_denies_edits_and_syncs_mode(hub):
    call("POST", "/api/away", {"away": False})
    call("POST", "/api/sessions/sess-1/mode", {"mode": "plan"})
    p = run_hook(None)
    out = finish(p, {**BASE, "hook_event_name": "PreToolUse", "tool_name": "Write",
                     "tool_input": {"file_path": "x.py", "content": ""}})
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    # the next permission prompt carries setMode so Claude's own mode follows
    payload = {**BASE, "hook_event_name": "PermissionRequest", "tool_name": "Bash", "tool_input": {"command": "ls"}}
    p = run_hook(payload)
    p.stdin.write(json.dumps(payload))
    p.stdin.close()
    req = wait_pending()[0]
    call("POST", f"/api/requests/{req['id']}/answer", {"answer": "✅ Allow"})
    out = json.loads(p.stdout.read())
    assert {"type": "setMode", "mode": "plan", "destination": "session"} in out["hookSpecificOutput"]["decision"]["updatedPermissions"]
    call("POST", "/api/sessions/sess-1/mode", {"mode": "clear"})


def test_ask_user_question_answered_from_phone(hub):
    call("POST", "/api/away", {"away": True})
    q = {"questions": [{"question": "Which DB?", "header": "DB", "multiSelect": False,
                        "options": [{"label": "Postgres", "description": "x"}, {"label": "SQLite", "description": "y"}]}]}
    payload = {**BASE, "hook_event_name": "PreToolUse", "tool_name": "AskUserQuestion", "tool_input": q}
    p = run_hook(payload)
    p.stdin.write(json.dumps(payload))
    p.stdin.close()
    req = wait_pending()[0]
    assert req["options"] == ["Postgres", "SQLite"]
    call("POST", f"/api/requests/{req['id']}/answer", {"answer": "SQLite"})
    out = json.loads(p.stdout.read())["hookSpecificOutput"]
    assert out["permissionDecision"] == "allow"
    assert out["updatedInput"]["answers"] == {"Which DB?": "SQLite"}
    call("POST", "/api/away", {"away": False})


def test_stop_reply_continues_agent(hub):
    call("POST", "/api/away", {"away": True})
    payload = {**BASE, "hook_event_name": "Stop", "stop_hook_active": False, "last_assistant_message": "Done with step 1"}
    p = run_hook(payload)
    p.stdin.write(json.dumps(payload))
    p.stdin.close()
    req = wait_pending()[0]
    assert req["kind"] == "reply"
    call("POST", f"/api/requests/{req['id']}/answer", {"answer": "now write tests"})
    out = json.loads(p.stdout.read())
    assert out["decision"] == "block" and "now write tests" in out["reason"]
    call("POST", "/api/away", {"away": False})


def test_task_runs_and_script_can_ask(hub):
    job = call("POST", "/api/run", {"task": "long-demo", "args": {"steps": "2"}})
    req = wait_pending()[0]
    assert req["title"].startswith("Halfway")
    call("POST", f"/api/requests/{req['id']}/answer", {"answer": "Continue"})
    for _ in range(100):
        j = call("GET", f"/api/jobs/{job['id']}")
        if j["status"] != "running":
            break
        time.sleep(0.1)
    assert j["status"] == "done", call("GET", f"/api/jobs/{job['id']}/log")["log"]
    assert "all done" in call("GET", f"/api/jobs/{job['id']}/log")["log"]


def test_confirm_task_waits_for_approval(hub):
    job = call("POST", "/api/run", {"task": "clean-tmp", "args": {"dir": "/nonexistent-dir-xyz"}})
    assert job["status"] == "waiting"
    req = wait_pending()[0]
    call("POST", f"/api/requests/{req['id']}/answer", {"answer": "❌ Cancel"})
    time.sleep(0.3)
    assert call("GET", f"/api/jobs/{job['id']}")["status"] == "cancelled"


def test_find(hub):
    res = call("GET", "/api/find?q=disk")["results"]
    assert any("disk-usage" in r for r in res)
