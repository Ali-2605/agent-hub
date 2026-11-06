"""The hub: one object that owns all state and behaviour.

    Telegram bot ─┐                         ┌─ jobs (your scripts, agent runs)
    REST API ─────┼──>  Hub  (this file)  ──┼─ requests (questions / approvals)
    MCP server ───┘                         └─ sessions (Claude Code / opencode)

The web server (server.py) and the Telegram bot (telegram.py) are thin layers
that call methods on Hub. If you want to add a feature, it usually goes here.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import html
import logging
import os
import signal
import time
from collections import deque
from pathlib import Path
from typing import Any, Awaitable, Callable, Protocol

from . import policy
from .config import Config
from .db import DB, new_id
from .tasks import Task, load_tasks, parse_schedule, resolve

log = logging.getLogger("agenthub")

Buttons = list[list[tuple[str, str]]]  # rows of (label, callback_data)


class Notifier(Protocol):
    async def send(self, text: str, buttons: Buttons | None = None) -> int | None: ...
    async def edit(self, message_id: int, text: str, buttons: Buttons | None = None) -> None: ...


class ConsoleNotifier:
    """Used when Telegram is not configured: prints what would be sent."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, Buttons | None]] = []

    async def send(self, text: str, buttons: Buttons | None = None) -> int | None:
        self.sent.append((text, buttons))
        log.info("NOTIFY: %s %s", text.replace("\n", " | ")[:300], buttons or "")
        return len(self.sent)

    async def edit(self, message_id: int, text: str, buttons: Buttons | None = None) -> None:
        log.info("EDIT %s: %s", message_id, text.replace("\n", " | ")[:300])


def esc(s: Any) -> str:
    return html.escape(str(s if s is not None else ""), quote=False)


def ago(ts: float | None) -> str:
    if not ts:
        return "-"
    s = int(time.time() - ts)
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m"
    if s < 86400:
        return f"{s // 3600}h"
    return f"{s // 86400}d"


def tail(path: str | Path, lines: int) -> str:
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - 64_000))
            data = f.read().decode(errors="replace")
        return "\n".join(deque(data.splitlines(), maxlen=lines))
    except FileNotFoundError:
        return ""


class Hub:
    def __init__(self, cfg: Config, notifier: Notifier | None = None):
        self.cfg = cfg
        self.db = DB(cfg.db_path)
        self.notifier: Notifier = notifier or ConsoleNotifier()
        self.tasks: dict[str, Task] = {}
        self.task_errors: list[str] = []
        self._events: dict[str, asyncio.Event] = {}
        self._on_answer: dict[str, Callable[[dict], Awaitable[None]]] = {}
        self._procs: dict[str, asyncio.subprocess.Process] = {}
        self._tickets: dict[str, asyncio.Task] = {}
        self._mode_changed = asyncio.Event()
        self._bg: list[asyncio.Task] = []
        # Set by `ahub serve`: stops the server. None when embedded (tests).
        self.on_shutdown: Callable[[], None] | None = None
        self.reload_tasks()

    # ================================================================ lifecycle
    async def start(self) -> None:
        for job in self.db.query("SELECT id FROM jobs WHERE status IN ('running', 'waiting')"):
            self.db.update("jobs", job["id"], status="lost", ended=time.time())
        for req in self.db.query("SELECT id FROM requests WHERE status = 'pending'"):
            # Their waiters died with the old process. Scripts re-poll and will see this.
            self.db.update("requests", req["id"], status="expired", answered_at=time.time())
        self._bg.append(asyncio.create_task(self._scheduler_loop()))
        self._bg.append(asyncio.create_task(self._expiry_loop()))

    async def stop(self) -> None:
        for t in self._bg:
            t.cancel()

    async def turn_off(self, source: str) -> bool:
        """Stop the hub (from `ahub off` or the phone's /off). Agents keep working
        normally while it is off (hooks fail open). Returns False if unsupported."""
        if self.on_shutdown is None:
            return False
        self.db.log_event("power", f"off ({source})")
        await self.notifier.send(f"🔴 agent-hub is <b>off</b> (turned off from {source}).\n"
                                 "Agents ask in the terminal again. To turn it on, run <code>ahub on</code> on the laptop.")
        asyncio.get_running_loop().call_later(0.3, self.on_shutdown)
        return True

    def reload_tasks(self) -> list[str]:
        self.tasks, self.task_errors = load_tasks(self.cfg.projects)
        return self.task_errors

    # ================================================================ settings
    @property
    def away(self) -> bool:
        return bool(self.db.setting("away", False))

    def set_away(self, away: bool) -> None:
        self.db.set_setting("away", away)
        self.db.log_event("away", "away" if away else "back")

    # ================================================================ notify
    async def notify(self, text: str, title: str | None = None, job_id: str | None = None) -> None:
        msg = (f"<b>{esc(title)}</b>\n" if title else "") + esc(text)
        if job_id:
            msg = f"[job <code>{esc(job_id)}</code>] " + msg
        self.db.log_event("notify", f"{title or ''} {text}", job_id or "")
        await self.notifier.send(msg)

    # ================================================================ requests
    # A "request" is anything that waits for you: a question, a permission
    # prompt, a "should I continue?" after an agent stopped.
    async def create_request(self, *, kind: str, title: str, body: str = "", options: list[str] | None = None,
                             allow_text: bool = False, source: str = "script", session_id: str | None = None,
                             job_id: str | None = None, meta: dict | None = None,
                             header: str | None = None) -> dict:
        rid = new_id("r")
        req = self.db.insert("requests", {
            "id": rid, "kind": kind, "source": source, "session_id": session_id, "job_id": job_id,
            "title": title, "body": body, "options": options or [], "allow_text": int(allow_text),
            "status": "pending", "created": time.time(), "meta": meta or {},
        })
        self._events[rid] = asyncio.Event()
        self.db.log_event("request", f"{kind}: {title} {body}", rid)
        msg_id = await self.notifier.send(self.render_request(req, header), self._request_buttons(req))
        if msg_id:
            self.db.update("requests", rid, tg_message_id=msg_id)
        return self.db.get("requests", rid)

    def render_request(self, req: dict, header: str | None = None) -> str:
        icon = {"approval": "🔐", "question": "❓", "reply": "💬"}.get(req["kind"], "❓")
        parts = []
        if header:
            parts.append(header)
        parts.append(f"{icon} <b>{esc(req['title'])}</b>")
        if req.get("body"):
            parts.append(f"<pre>{esc(req['body'][:3000])}</pre>")
        if req["status"] == "pending":
            if req["allow_text"]:
                parts.append("<i>↩️ Reply to this message to answer with text.</i>")
        else:
            label = {"answered": "✅ Answered", "expired": "⌛ Expired", "cancelled": "🚫 Cancelled"}[req["status"]]
            parts.append(f"<b>{label}</b>" + (f": {esc(req['answer'])}" if req.get("answer") else ""))
        parts.append(f"<code>{req['id']}</code>")
        return "\n".join(parts)

    @staticmethod
    def _request_buttons(req: dict) -> Buttons | None:
        opts = req.get("options") or []
        if not opts:
            return None
        rows, row = [], []
        for i, label in enumerate(opts):
            row.append((label, f"a:{req['id']}:{i}"))
            if len(row) == 3:
                rows.append(row)
                row = []
        if row:
            rows.append(row)
        return rows

    async def answer_request(self, rid: str, answer: str, status: str = "answered") -> dict | None:
        req = self.db.find("requests", rid)
        if not req or req["status"] != "pending":
            return req
        req = self.db.update("requests", req["id"], status=status, answer=answer, answered_at=time.time())
        self.db.log_event("answer", f"{req['title']} -> {answer}", req["id"])
        if ev := self._events.pop(req["id"], None):
            ev.set()
        if req.get("tg_message_id"):
            try:
                await self.notifier.edit(req["tg_message_id"], self.render_request(req), None)
            except Exception as e:  # noqa: BLE001
                log.warning("could not edit message: %s", e)
        if cb := self._on_answer.pop(req["id"], None):
            asyncio.create_task(cb(req))
        return req

    async def answer_option(self, rid: str, index: int) -> dict | None:
        req = self.db.find("requests", rid)
        if not req:
            return None
        opts = req.get("options") or []
        if 0 <= index < len(opts):
            return await self.answer_request(req["id"], opts[index])
        return req

    async def wait_request(self, rid: str, timeout: float) -> dict | None:
        req = self.db.find("requests", rid)
        if not req or req["status"] != "pending":
            return req
        ev = self._events.setdefault(req["id"], asyncio.Event())
        try:
            await asyncio.wait_for(ev.wait(), timeout)
        except asyncio.TimeoutError:
            pass
        return self.db.get("requests", req["id"])

    async def ask(self, **kwargs: Any) -> dict:
        """Create a request and wait (forever, or until expiry) for the answer."""
        req = await self.create_request(**kwargs)
        while True:
            req = await self.wait_request(req["id"], 3600)
            if req["status"] != "pending":
                return req

    def pending_requests(self) -> list[dict]:
        return self.db.query("SELECT * FROM requests WHERE status = 'pending' ORDER BY created")

    async def _expiry_loop(self) -> None:
        while True:
            await asyncio.sleep(30)
            minutes = self.cfg.request_timeout_minutes
            if minutes <= 0:
                continue
            cutoff = time.time() - minutes * 60
            for req in self.db.query("SELECT * FROM requests WHERE status='pending' AND created < ? AND kind != 'reply'",
                                     [cutoff]):
                await self.answer_request(req["id"], f"(timed out → {self.cfg.on_timeout})", status="expired")

    # ================================================================ sessions
    def upsert_session(self, sid: str, agent: str, **fields: Any) -> dict:
        now = time.time()
        if not self.db.get("sessions", sid):
            self.db.insert("sessions", {"id": sid, "agent": agent, "routing": "auto", "status": "active",
                                        "created": now, "last_seen": now})
        fields = {k: v for k, v in fields.items() if v is not None}
        return self.db.update("sessions", sid, last_seen=now, **fields)

    def list_sessions(self, include_ended: bool = False, hours: int = 24) -> list[dict]:
        cutoff = time.time() - hours * 3600
        sql = "SELECT * FROM sessions WHERE last_seen > ?" + ("" if include_ended else " AND status != 'ended'")
        return self.db.query(sql + " ORDER BY last_seen DESC", [cutoff])

    def set_session_mode(self, sid: str, mode: str | None) -> dict | None:
        s = self.db.find("sessions", sid)
        if not s:
            return None
        if mode in (None, "clear", "none", "off"):
            s = self.db.update("sessions", s["id"], override_mode=None, pending_set_mode=None)
        else:
            if mode not in policy.MODES:
                raise ValueError(f"unknown mode '{mode}'. Use one of: {', '.join(policy.MODES)}")
            if s["agent"] == "claude":
                pending = mode if mode in policy.CLAUDE_MODES else None
            else:  # opencode: switch the session's agent (plan / build)
                pending = None if mode == "pause" else ("plan" if mode == "plan" else "build")
            s = self.db.update("sessions", s["id"], override_mode=mode, pending_set_mode=pending)
        self.db.log_event("mode", f"{s['id']} -> {mode}", s["id"])
        self._mode_changed.set()
        self._mode_changed = asyncio.Event()
        return s

    def set_session_routing(self, sid: str, routing: str) -> dict | None:
        if routing not in ("auto", "phone", "local"):
            raise ValueError("routing must be auto, phone or local")
        s = self.db.find("sessions", sid)
        return self.db.update("sessions", s["id"], routing=routing) if s else None

    def session_label(self, s: dict) -> str:
        where = Path(s.get("cwd") or "?").name
        title = s.get("title") or ""
        return f"{s['agent']} · {where}" + (f" · {title[:30]}" if title else "")

    def _routed(self, s: dict, forced: bool) -> bool:
        """Should questions/permission prompts for this session go to the phone?
        A mode picked on the phone implies yes (you are probably not at the desk)."""
        return policy.is_routed(s.get("routing"), self.away, forced or bool(s.get("override_mode")))

    def _converse(self, s: dict, forced: bool) -> bool:
        """Should a finished turn wait for a phone reply? Only when away / routed to phone,
        not merely because a mode override is set."""
        return policy.is_routed(s.get("routing"), self.away, forced)

    # ================================================================ hook tickets
    # A hook asks "what should I do?". If the answer needs you, the hub returns
    # a ticket and the hook polls /api/hook/wait/<ticket> until it is resolved.
    def _ticket(self, coro: Awaitable[dict | None]) -> dict:
        tid = new_id("t")
        self._tickets[tid] = asyncio.ensure_future(coro)
        return {"pending": tid}

    async def wait_ticket(self, tid: str, timeout: float) -> dict | None:
        task = self._tickets.get(tid)
        if task is None:
            return None
        try:
            result = await asyncio.wait_for(asyncio.shield(task), timeout)
        except asyncio.TimeoutError:
            return {"pending": tid}
        self._tickets.pop(tid, None)
        return {"output": result}

    async def _hold_while_paused(self, sid: str) -> dict:
        while True:
            s = self.db.get("sessions", sid)
            if not s or s.get("override_mode") != "pause":
                return s
            try:
                await asyncio.wait_for(self._mode_changed.wait(), 30)
            except asyncio.TimeoutError:
                pass

    # ================================================================ Claude Code
    async def handle_claude_hook(self, p: dict, forced: bool = False) -> dict:
        """Entry point for every Claude Code hook event. Returns {"output": ...} or {"pending": ticket}."""
        sid = p.get("session_id") or "unknown"
        event = p.get("hook_event_name", "")
        s = self.upsert_session(sid, "claude", cwd=p.get("cwd"), agent_mode=p.get("permission_mode"),
                                last_event=event, title=p.get("session_title"))
        if forced and s.get("routing") == "auto":
            s = self.db.update("sessions", sid, routing="phone")
        routed = self._routed(s, forced)
        converse = self._converse(s, forced)

        if event == "SessionStart":
            self.db.update("sessions", sid, status="active")
        elif event == "SessionEnd":
            self.db.update("sessions", sid, status="ended")
        elif event == "UserPromptSubmit":
            self.db.update("sessions", sid, last_message="You: " + str(p.get("prompt", ""))[:300])
        elif event == "Notification":
            ntype = p.get("notification_type", "")
            if converse and ntype not in ("permission_prompt", "idle_prompt"):
                await self.notifier.send(f"🔔 <b>{esc(self.session_label(s))}</b>\n{esc(p.get('message', ''))}")
        elif event == "PreToolUse":
            return await self._claude_pre_tool(s, p, routed)
        elif event == "PermissionRequest":
            if routed:
                return self._ticket(self._claude_permission(s, p))
        elif event == "Stop":
            last = str(p.get("last_assistant_message") or "")
            self.db.update("sessions", sid, last_message=last[:1000], status="idle")
            if converse:
                return self._ticket(self._stop_conversation(s, last, "claude"))
        return {"output": None}

    async def _claude_pre_tool(self, s: dict, p: dict, routed: bool) -> dict:
        tool, tool_input = p.get("tool_name", ""), p.get("tool_input") or {}
        self.db.update("sessions", s["id"], status="working")
        if tool == "AskUserQuestion":
            return self._ticket(self._claude_questions(s, tool_input)) if routed else {"output": None}
        decision = policy.tool_decision("claude", tool, s.get("override_mode"))
        if decision == "pass":
            return {"output": None}
        if decision in ("hold", "ask"):
            return self._ticket(self._claude_pre_tool_wait(s, p))
        return {"output": self._pre_tool_output(decision, f"Mode '{s['override_mode']}' was set by the user from their phone.")}

    async def _claude_pre_tool_wait(self, s: dict, p: dict) -> dict | None:
        tool, tool_input = p.get("tool_name", ""), p.get("tool_input") or {}
        decision = policy.tool_decision("claude", tool, s.get("override_mode"))
        if decision == "hold":
            s = await self._hold_while_paused(s["id"])
            decision = policy.tool_decision("claude", tool, (s or {}).get("override_mode"))
        if decision == "pass":
            return None
        if decision != "ask":
            return self._pre_tool_output(decision, f"Mode '{s.get('override_mode')}' was set by the user from their phone.")
        req = await self.ask(kind="approval", title="Allow this?", body=policy.describe_tool_call(tool, tool_input),
                             options=["✅ Allow", "❌ Deny"], allow_text=True, source="claude", session_id=s["id"],
                             header=self._session_header(s))
        allowed, reason = self._interpret_approval(req)
        return self._pre_tool_output("allow" if allowed else "deny", reason)

    @staticmethod
    def _pre_tool_output(decision: str, reason: str) -> dict:
        return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": decision,
                                       "permissionDecisionReason": reason}}

    def _interpret_approval(self, req: dict) -> tuple[bool, str]:
        answer = req.get("answer") or ""
        if req["status"] == "expired":
            return self.cfg.on_timeout == "allow", "The user did not answer in time."
        if answer.startswith("✅"):
            return True, "Approved by the user from their phone."
        if answer.startswith("❌"):
            return False, "The user denied this from their phone."
        # Free text = denied, with instructions for the agent.
        return False, f"The user did not approve this and replied from their phone: {answer}"

    def _session_header(self, s: dict) -> str:
        mode = s.get("override_mode") or s.get("agent_mode")
        return f"{policy.MODE_EMOJI.get(mode, '·')} <b>{esc(self.session_label(s))}</b> <i>({esc(mode or '?')})</i>"

    async def _claude_permission(self, s: dict, p: dict) -> dict | None:
        tool, tool_input = p.get("tool_name", ""), p.get("tool_input") or {}
        options = ["✅ Allow", "❌ Deny"]
        suggestions = p.get("permission_suggestions") or []
        if suggestions:
            options.insert(1, "✅ Always allow")
        req = await self.ask(kind="approval", title="Permission needed", body=policy.describe_tool_call(tool, tool_input),
                             options=options, allow_text=True, source="claude", session_id=s["id"],
                             header=self._session_header(s))
        allowed, reason = self._interpret_approval(req)
        if not allowed:
            return {"hookSpecificOutput": {"hookEventName": "PermissionRequest",
                                           "decision": {"behavior": "deny", "message": reason}}}
        decision: dict[str, Any] = {"behavior": "allow"}
        updates = list(suggestions) if req.get("answer") == "✅ Always allow" else []
        fresh = self.db.get("sessions", s["id"]) or s
        if fresh.get("pending_set_mode"):
            # This is the moment Claude lets us change its real permission mode.
            updates.append({"type": "setMode", "mode": fresh["pending_set_mode"], "destination": "session"})
            self.db.update("sessions", s["id"], pending_set_mode=None)
        if updates:
            decision["updatedPermissions"] = updates
        return {"hookSpecificOutput": {"hookEventName": "PermissionRequest", "decision": decision}}

    async def _claude_questions(self, s: dict, tool_input: dict) -> dict | None:
        """Answer Claude's AskUserQuestion from the phone (one message per question)."""
        questions = tool_input.get("questions") or []
        answers: dict[str, str] = {}
        for q in questions:
            labels = [o.get("label", str(o)) if isinstance(o, dict) else str(o) for o in q.get("options") or []]
            body = "\n".join(
                f"• {o.get('label')}: {o.get('description', '')}" for o in q.get("options") or [] if isinstance(o, dict)
            )
            req = await self.ask(kind="question", title=q.get("question", "?"), body=body, options=labels,
                                 allow_text=True, source="claude", session_id=s["id"], header=self._session_header(s))
            answers[q.get("question", "?")] = req.get("answer") or ""
        return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "allow",
                                       "permissionDecisionReason": "Answered by the user from their phone.",
                                       "updatedInput": {**tool_input, "answers": answers}}}

    async def _stop_conversation(self, s: dict, last_message: str, agent: str) -> dict | None:
        """Agent finished its turn: show you the result and let you reply to keep it going."""
        req = await self.create_request(
            kind="reply", title="Finished — reply to continue", body=last_message[-3000:] or "(no text)",
            options=["✅ Done"], allow_text=True, source=agent, session_id=s["id"], header=self._session_header(s))
        req = await self.wait_request(req["id"], self.cfg.stop_wait_minutes * 60)
        if req["status"] == "pending":
            await self.answer_request(req["id"], "(no reply — agent stopped)", status="expired")
            return None
        answer = req.get("answer") or ""
        if req["status"] != "answered" or answer == "✅ Done":
            return None
        self.db.update("sessions", s["id"], status="working", last_message="You: " + answer[:300])
        reason = f"New message from the user (sent from their phone): {answer}"
        if agent == "claude":
            return {"decision": "block", "reason": reason}
        return {"reply": answer}

    # ================================================================ opencode
    async def handle_opencode_hook(self, p: dict, forced: bool = False) -> dict:
        resp = await self._handle_opencode_hook(p, forced)
        s = self.db.get("sessions", p.get("sessionID") or "unknown")
        if s and s.get("pending_set_mode"):
            # The plugin switches the opencode agent (e.g. to "plan") when it sees this.
            resp = {**resp, "switch_agent": s["pending_set_mode"]}
            self.db.update("sessions", s["id"], pending_set_mode=None)
        return resp

    async def _handle_opencode_hook(self, p: dict, forced: bool) -> dict:
        sid = p.get("sessionID") or "unknown"
        event = p.get("event", "")
        s = self.upsert_session(sid, "opencode", cwd=p.get("directory"), agent_mode=p.get("agent"),
                                last_event=event, title=p.get("title"))
        if forced and s.get("routing") == "auto":
            s = self.db.update("sessions", sid, routing="phone")
        routed = self._routed(s, forced)

        if event == "chat":
            self.db.update("sessions", sid, status="working", last_message="You: " + str(p.get("text", ""))[:300])
        elif event == "tool.before":
            tool = p.get("tool", "")
            decision = policy.tool_decision("opencode", tool, s.get("override_mode"))
            if decision in ("hold", "ask"):
                return self._ticket(self._opencode_tool_wait(s, tool, p.get("args") or {}))
            if decision == "deny":
                return {"output": {"action": "deny", "reason": f"Mode '{s['override_mode']}' was set by the user from their phone."}}
        elif event == "permission.ask":
            if routed:
                return self._ticket(self._opencode_permission(s, p))
        elif event == "idle":
            last = str(p.get("lastMessage") or "")
            self.db.update("sessions", sid, status="idle", last_message=last[:1000] or None)
            if self._converse(s, forced) and not p.get("job"):
                return self._ticket(self._stop_conversation(s, last, "opencode"))
        elif event == "error":
            if self._converse(s, forced):
                await self.notifier.send(f"⚠️ <b>{esc(self.session_label(s))}</b> error\n<pre>{esc(str(p.get('error'))[:1500])}</pre>")
        elif event == "deleted":
            self.db.update("sessions", sid, status="ended")
        return {"output": None}

    async def _opencode_tool_wait(self, s: dict, tool: str, args: dict) -> dict | None:
        decision = policy.tool_decision("opencode", tool, s.get("override_mode"))
        if decision == "hold":
            s = await self._hold_while_paused(s["id"])
            decision = policy.tool_decision("opencode", tool, (s or {}).get("override_mode"))
        if decision == "deny":
            return {"action": "deny", "reason": f"Mode '{s.get('override_mode')}' was set by the user from their phone."}
        if decision != "ask":
            return None
        req = await self.ask(kind="approval", title="Allow this?", body=policy.describe_tool_call(tool, args),
                             options=["✅ Allow", "❌ Deny"], allow_text=True, source="opencode",
                             session_id=s["id"], header=self._session_header(s))
        allowed, reason = self._interpret_approval(req)
        return None if allowed else {"action": "deny", "reason": reason}

    async def _opencode_permission(self, s: dict, p: dict) -> dict | None:
        req = await self.ask(kind="approval", title="Permission needed",
                             body=f"{p.get('type', '')}: {p.get('title', '')} {p.get('pattern') or ''}".strip(),
                             options=["✅ Allow", "❌ Deny"], allow_text=True, source="opencode",
                             session_id=s["id"], header=self._session_header(s))
        allowed, reason = self._interpret_approval(req)
        return {"status": "allow" if allowed else "deny", "reason": reason}

    # ================================================================ jobs
    async def run_task(self, ref: str, values: dict[str, str] | None = None, source: str = "api",
                       confirmed: bool = False) -> dict:
        task = resolve(self.tasks, ref)
        if not task:
            raise ValueError(f"no task named '{ref}'. Try /tasks")
        argv = task.build_argv(values or {})  # validates args before asking
        if task.confirm and not confirmed:
            job = self._new_job(task.id, task.project, "task", argv, task.cwd, "waiting", task.notify, task.timeout)
            req = await self.create_request(kind="approval", title=f"Run {task.id}?", body=" ".join(argv),
                                            options=["✅ Run", "❌ Cancel"], source="task", job_id=job["id"])

            async def on_answer(r: dict) -> None:
                if r.get("answer") == "✅ Run":
                    await self._spawn(job["id"])
                else:
                    self.db.update("jobs", job["id"], status="cancelled", ended=time.time())

            self._on_answer[req["id"]] = on_answer
            return job
        job = self._new_job(task.id, task.project, "task", argv, task.cwd, "starting", task.notify, task.timeout)
        await self._spawn(job["id"])
        return self.db.get("jobs", job["id"])

    async def run_agent(self, agent: str, project: str, prompt: str, mode: str | None = None,
                        session_id: str | None = None) -> dict:
        """Start a Claude Code / opencode run in a project folder (from the phone)."""
        root = self.cfg.projects.get(project)
        if root is None:
            p = Path(os.path.expanduser(project))
            if not p.is_dir():
                raise ValueError(f"unknown project '{project}'. Known: {', '.join(self.cfg.projects) or 'none'}")
            root = p.resolve()
        if agent == "claude":
            mode = mode or self.cfg.phone_claude_mode
            argv = ["claude", "-p", prompt, "--permission-mode", mode]
        elif agent == "opencode":
            # --standalone: a private server that sees our env (AGENT_HUB_ROUTE), unlike the
            # background service. Our own session id lets you continue the conversation later.
            session_id = session_id or "ses_hub" + os.urandom(10).hex()
            argv = ["opencode", "run", "--standalone", "-s", session_id] + (["--agent", mode] if mode else []) + [prompt]
        else:
            raise ValueError("agent must be claude or opencode")
        job = self._new_job(f"{agent}:{prompt[:40]}", project, agent, argv, root, "starting", "always", 0)
        self.db.update("jobs", job["id"], meta={"timeout": 0, "agent_session": session_id, "mode": mode})
        await self._spawn(job["id"], extra_env={"AGENT_HUB_ROUTE": "phone"})
        return self.db.get("jobs", job["id"])

    async def _offer_continue(self, job: dict) -> None:
        """opencode runs exit when done, so 'reply to continue' starts a new run in the same session."""
        meta = job.get("meta") or {}
        req = await self.create_request(
            kind="reply", title=f"opencode finished ({job['project']}) — reply to continue",
            body=tail(job["log_path"], 25)[-2500:], options=["✅ Done"], allow_text=True,
            source="opencode", job_id=job["id"])

        async def on_answer(r: dict) -> None:
            answer = r.get("answer") or ""
            if r["status"] == "answered" and answer != "✅ Done":
                await self.run_agent("opencode", job["project"], answer, meta.get("mode"), meta.get("agent_session"))

        self._on_answer[req["id"]] = on_answer

    def _new_job(self, task: str, project: str, kind: str, argv: list[str], cwd: Path, status: str,
                 notify: str, timeout: int) -> dict:
        jid = new_id("j")
        return self.db.insert("jobs", {
            "id": jid, "task": task, "project": project, "kind": kind, "command": argv, "cwd": str(cwd),
            "status": status, "log_path": str(self.cfg.logs_dir / f"{jid}.log"), "notify": notify,
            "meta": {"timeout": timeout}, "started": time.time(),
        })

    async def _spawn(self, jid: str, extra_env: dict | None = None) -> None:
        job = self.db.get("jobs", jid)
        # PYTHONPATH lets any python3 (even outside our venv) `import agenthub.client` (stdlib-only).
        pkg_root = str(Path(__file__).resolve().parent.parent)
        pythonpath = os.pathsep.join(p for p in (pkg_root, os.environ.get("PYTHONPATH", "")) if p)
        env = {**os.environ, "AGENT_HUB_JOB_ID": jid, "PYTHONUNBUFFERED": "1", "PYTHONPATH": pythonpath,
               **(extra_env or {})}
        logf = open(job["log_path"], "ab")
        try:
            proc = await asyncio.create_subprocess_exec(
                *job["command"], cwd=job["cwd"], stdout=logf, stderr=asyncio.subprocess.STDOUT,
                stdin=asyncio.subprocess.DEVNULL, env=env, start_new_session=True)
        except Exception as e:  # noqa: BLE001
            logf.write(f"failed to start: {e}\n".encode())
            logf.close()
            self.db.update("jobs", jid, status="failed", exit_code=-1, ended=time.time())
            await self.notifier.send(f"❌ <b>{esc(job['task'])}</b> could not start: {esc(e)}")
            return
        self._procs[jid] = proc
        self.db.update("jobs", jid, status="running", pid=proc.pid, started=time.time())
        self.db.log_event("job", f"started {job['task']}", jid)
        if job["kind"] != "task" or job["notify"] == "always":
            await self.notifier.send(f"▶️ Started <b>{esc(job['task'])}</b> <code>{jid}</code>")
        asyncio.create_task(self._watch(jid, proc, logf, (job.get("meta") or {}).get("timeout") or 0))

    async def _watch(self, jid: str, proc: asyncio.subprocess.Process, logf: Any, timeout: int) -> None:
        timed_out = False
        try:
            if timeout:
                await asyncio.wait_for(proc.wait(), timeout)
            else:
                await proc.wait()
        except asyncio.TimeoutError:
            timed_out = True
            await self._kill(proc)
        logf.close()
        self._procs.pop(jid, None)
        job = self.db.get("jobs", jid)
        if job["status"] == "stopped":
            status = "stopped"
        elif timed_out:
            status = "timeout"
        else:
            status = "done" if proc.returncode == 0 else "failed"
        job = self.db.update("jobs", jid, status=status, exit_code=proc.returncode, ended=time.time())
        self.db.log_event("job", f"{status} {job['task']} exit={proc.returncode}", jid)
        if job["kind"] == "opencode" and status in ("done", "failed"):
            await self._offer_continue(job)
            return
        if job["notify"] == "never" or (job["notify"] == "failure" and status == "done"):
            return
        icon = {"done": "✅", "failed": "❌", "timeout": "⌛", "stopped": "⏹"}[status]
        out = tail(job["log_path"], 15)
        msg = f"{icon} <b>{esc(job['task'])}</b> {status} (exit {proc.returncode}, {self._duration(job)}) <code>{jid}</code>"
        if out:
            msg += f"\n<pre>{esc(out[-2500:])}</pre>"
        await self.notifier.send(msg, [[("📜 Full log", f"log:{jid}")]])

    @staticmethod
    def _duration(job: dict) -> str:
        secs = int((job.get("ended") or time.time()) - (job.get("started") or time.time()))
        return f"{secs // 60}m{secs % 60:02d}s" if secs >= 60 else f"{secs}s"

    async def _kill(self, proc: asyncio.subprocess.Process) -> None:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
            try:
                await asyncio.wait_for(proc.wait(), 5)
            except asyncio.TimeoutError:
                os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    async def stop_job(self, jid: str) -> dict | None:
        job = self.db.find("jobs", jid)
        if not job:
            return None
        proc = self._procs.get(job["id"])
        if proc:
            self.db.update("jobs", job["id"], status="stopped")
            await self._kill(proc)
        return self.db.get("jobs", job["id"])

    def set_progress(self, jid: str, percent: float | None, text: str) -> None:
        self.db.update("jobs", jid, progress=percent, progress_text=text[:300])

    def recent_jobs(self, limit: int = 10) -> list[dict]:
        return self.db.query("SELECT * FROM jobs ORDER BY started DESC LIMIT ?", [limit])

    def job_log(self, jid: str, lines: int | None = None) -> tuple[dict | None, str]:
        job = self.db.find("jobs", jid)
        if not job:
            return None, ""
        return job, tail(job["log_path"], lines or self.cfg.log_tail_lines)

    # ================================================================ scheduler
    async def _scheduler_loop(self) -> None:
        while True:
            await asyncio.sleep(20)
            now = time.time()
            for task in list(self.tasks.values()):
                if not task.schedule:
                    continue
                try:
                    kind, value = parse_schedule(task.schedule)
                except ValueError as e:
                    log.warning("%s: %s", task.id, e)
                    continue
                key = f"sched:{task.id}"
                last = self.db.setting(key, 0)
                if kind == "every":
                    due = now - last >= value
                    if not last:  # first sighting: start the clock, don't fire at boot
                        self.db.set_setting(key, now)
                        continue
                else:
                    h, m = value
                    today = dt.datetime.now().replace(hour=h, minute=m, second=0, microsecond=0).timestamp()
                    due = now >= today and last < today
                if due:
                    self.db.set_setting(key, now)
                    try:
                        await self.run_task(task.id, source="schedule")
                    except Exception as e:  # noqa: BLE001
                        await self.notifier.send(f"⚠️ scheduled task {esc(task.id)} failed to start: {esc(e)}")

    # ================================================================ search
    def find(self, query: str, limit: int = 8) -> list[str]:
        """Plain text search across tasks, job logs, agent messages and history. No AI."""
        q = query.lower()
        hits: list[str] = []
        for t in self.tasks.values():
            if q in t.id.lower() or q in t.description.lower() or q in " ".join(t.tags).lower():
                hits.append(f"🧰 task <b>{esc(t.id)}</b> — {esc(t.description[:120])}")
        for s in self.list_sessions(include_ended=True, hours=24 * 7):
            if q in (s.get("last_message") or "").lower() or q in (s.get("cwd") or "").lower():
                hits.append(f"🤖 {esc(self.session_label(s))} <code>{s['id'][:8]}</code>: "
                            f"{esc(self._snippet(s.get('last_message') or '', q))}")
        for job in self.recent_jobs(100):
            text = tail(job["log_path"], 2000)
            if q in text.lower() or q in job["task"].lower():
                hits.append(f"📜 job <code>{job['id']}</code> {esc(job['task'])} ({job['status']}, {ago(job['started'])} ago): "
                            f"{esc(self._snippet(text, q))}")
        for ev in self.db.query("SELECT * FROM events WHERE lower(text) LIKE ? ORDER BY ts DESC LIMIT 20", [f"%{q}%"]):
            hits.append(f"🕘 {ago(ev['ts'])} ago {ev['kind']}: {esc(self._snippet(ev['text'], q))}")
        return hits[:limit]

    @staticmethod
    def _snippet(text: str, q: str, width: int = 90) -> str:
        i = text.lower().find(q)
        if i < 0:
            return text[:width]
        start = max(0, i - width // 2)
        return ("…" if start else "") + text[start:start + width].replace("\n", " ") + "…"

    # ================================================================ status
    def status(self) -> dict:
        return {
            "away": self.away,
            "running_jobs": self.db.query("SELECT * FROM jobs WHERE status IN ('running','waiting') ORDER BY started"),
            "sessions": self.list_sessions(),
            "pending": self.pending_requests(),
            "tasks": len(self.tasks),
            "task_errors": self.task_errors,
        }
