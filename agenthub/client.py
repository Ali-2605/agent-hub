"""Tiny stdlib-only client for the hub. Use it from ANY Python script:

    from agenthub.client import notify, ask, progress, send_file

    notify("Backup started")
    if ask("Delete 1,204 old files?", options=["Yes", "No"]) == "Yes":
        ...
    progress(40, "Copied 400/1000 files")

If the hub is not running, notify()/progress() silently do nothing and ask()
raises HubUnavailable (so a script never hangs or crashes by surprise).

Not using Python? The same things exist as shell commands:
    ahub notify "text"      ahub ask "question" -o Yes -o No      ahub progress 40 "msg"
    ahub send report.pdf -c "caption"
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from typing import Any

from .config import api_token, hub_url


class HubUnavailable(RuntimeError):
    pass


def call(method: str, path: str, body: dict | None = None, timeout: float = 10) -> Any:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        hub_url() + path,
        data=data,
        method=method,
        headers={"Content-Type": "application/json", "X-Hub-Token": api_token()},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            return json.loads(raw) if raw else None
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")
        raise RuntimeError(f"hub returned {e.code}: {detail}") from e
    except (urllib.error.URLError, ConnectionError, TimeoutError, OSError) as e:
        raise HubUnavailable(f"agent-hub is not reachable at {hub_url()} ({e})") from e


def is_up() -> bool:
    try:
        call("GET", "/api/health", timeout=1.5)
        return True
    except Exception:
        return False


def wait_for(request_id: str, timeout_s: float | None = None) -> dict:
    """Block until a request (question/approval) is answered. Returns the request dict."""
    deadline = None if timeout_s is None else time.time() + timeout_s
    while True:
        try:
            req = call("GET", f"/api/requests/{request_id}/wait?timeout=50", timeout=60)
        except HubUnavailable:
            time.sleep(3)  # hub restarting; keep waiting
            continue
        if req["status"] != "pending":
            return req
        if deadline and time.time() > deadline:
            return req


def notify(text: str, title: str | None = None) -> None:
    try:
        call("POST", "/api/notify", {"text": text, "title": title, "job_id": os.environ.get("AGENT_HUB_JOB_ID")})
    except HubUnavailable:
        pass


def send_file(path: str, caption: str = "") -> bool:
    """Send a file (report, chart, CSV…) to the phone. Up to 50 MB. Returns False if the
    hub is off. Raises RuntimeError if the hub refuses it (missing, too big, secret)."""
    try:
        call("POST", "/api/files", {"path": os.path.abspath(os.path.expanduser(path)), "caption": caption,
                                    "job_id": os.environ.get("AGENT_HUB_JOB_ID")}, timeout=310)
        return True
    except HubUnavailable:
        return False


def progress(percent: float | None, text: str = "") -> None:
    """Update the progress of the current job (shown in /jobs). No push notification."""
    job_id = os.environ.get("AGENT_HUB_JOB_ID")
    if not job_id:
        return
    try:
        call("POST", f"/api/jobs/{job_id}/progress", {"percent": percent, "text": text})
    except HubUnavailable:
        pass


def ask(question: str, options: list[str] | None = None, allow_text: bool | None = None,
        timeout_s: float | None = None, default: str | None = None) -> str | None:
    """Ask on the phone and wait. Returns the chosen option or typed text.

    options      buttons to show (e.g. ["Yes", "No"]). None = free-text answer.
    allow_text   also accept a typed reply (default: True when no options given).
    timeout_s    give up after this many seconds and return `default`.
    """
    req = call("POST", "/api/requests", {
        "kind": "question",
        "source": os.environ.get("AGENT_HUB_JOB_ID") and "job" or "script",
        "job_id": os.environ.get("AGENT_HUB_JOB_ID"),
        "title": question,
        "options": options or [],
        "allow_text": allow_text if allow_text is not None else not options,
    })
    done = wait_for(req["id"], timeout_s)
    if done["status"] != "answered":
        return default
    return done["answer"]


def confirm(question: str, timeout_s: float | None = None, default: bool = False) -> bool:
    answer = ask(question, options=["Yes", "No"], timeout_s=timeout_s, default="Yes" if default else "No")
    return answer == "Yes"
