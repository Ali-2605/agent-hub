"""Telegram bot: your phone's remote control.

Talks to the Telegram Bot API directly with httpx (long polling, no webhook,
no open ports). Only private-chat messages from TELEGRAM_CHAT_ID are accepted;
everyone else is ignored.

Phone lock: the phone is locked by default. Only the read-only commands in
OPEN_COMMANDS (plus /unlock and /off) work while locked. Everything else, including
buttons and text replies to agents, needs `/unlock <PIN>` first (PIN set on the
laptop with `ahub pin`). Too many wrong PINs turn the hub off.

To add a command: write `async def cmd_<name>(self, args: str)` below and add
a line to HELP. Dispatch is automatic, and new commands are locked unless you add
them to OPEN_COMMANDS (only do that for read-only commands).
"""
from __future__ import annotations

import asyncio
import logging
import shlex
import time
from pathlib import Path
from typing import TYPE_CHECKING

import httpx

from . import policy
from .hub import Buttons, ago, esc
from .lock import check_pin, has_pin

if TYPE_CHECKING:
    from .hub import Hub

log = logging.getLogger("agenthub.telegram")
MAX_LEN = 4000

# Work while the phone is locked. Read-only, plus the kill switch. Everything else is locked.
OPEN_COMMANDS = {"start", "help", "status", "pending", "unlock", "lock", "off"}

HELP = """<b>agent-hub</b> — commands

<b>Overview</b>
/status — away state, running jobs, agents, pending questions
/away — send agent questions &amp; permissions to the phone
/back — let them go to the terminal again
/pending — everything waiting for your answer

<b>Scripts</b>
/tasks — list scripts (tap to run)
/run name key=value … — run a script
/jobs — recent runs   /log id — output   /stop id — kill

<b>Agents</b>
/sessions — Claude Code / opencode sessions with their mode
/mode id mode — change mode (default, acceptEdits, plan, auto, bypassPermissions, pause, clear)
/route id auto|phone|local — where this session asks questions
/msg id prompt — send a new prompt to a session (or tap 💬 in /sessions)
/claude project prompt — start Claude Code in a project
/opencode project prompt — start opencode in a project
/projects — known project folders

<b>Find</b>
/find text — search tasks, logs, agent messages, history
/get project path — send me a file from a project folder
/reload — reload task files

<b>Security</b>
/unlock PIN — unlock the phone for a few minutes (PIN set on the laptop: <code>ahub pin</code>)
/lock — lock it again now

<b>Power</b>
/off — turn the hub off (turn it on again on the laptop: <code>ahub on</code>)

Answer questions with the buttons, or <b>reply</b> to the message with text."""


class TelegramBot:
    def __init__(self, hub: "Hub", token: str, chat_id: int):
        self.hub = hub
        self.chat_id = chat_id
        self._token = token
        self.api = f"https://api.telegram.org/bot{token}"
        self.http = httpx.AsyncClient(timeout=70)
        self._offset = 0
        # Phone lock state lives in memory only: a restart always starts locked.
        self._unlocked_until = 0.0
        self._failed_pins = 0
        self._blocked = False  # too many wrong PINs and the hub could not turn itself off
        self._conflict_warned = False

    # ------------------------------------------------------------ phone lock
    @property
    def unlocked(self) -> bool:
        return not self._blocked and time.monotonic() < self._unlocked_until

    def lock(self) -> None:
        self._unlocked_until = 0.0

    def _redact(self, text: object) -> str:
        """Never let the bot token reach a log line or a chat message."""
        out = str(text)
        return out.replace(self._token, "<bot-token>") if self._token else out

    def _is_owner(self, chat: dict, sender: dict | None) -> bool:
        return (chat.get("id") == self.chat_id and chat.get("type") == "private"
                and (sender or {}).get("id") == self.chat_id)

    # ------------------------------------------------------------ Notifier API
    async def send(self, text: str, buttons: Buttons | None = None, reply_to: int | None = None) -> int | None:
        if buttons and not self.unlocked:
            text += "\n\n🔒 <i>Phone locked: /unlock PIN to answer.</i>"
        payload: dict = {"chat_id": self.chat_id, "text": self._fit(text), "parse_mode": "HTML",
                         "disable_web_page_preview": True}
        if buttons:
            payload["reply_markup"] = self._markup(buttons)
        if reply_to:
            payload["reply_parameters"] = {"message_id": reply_to, "allow_sending_without_reply": True}
        data = await self._call("sendMessage", payload)
        return data["message_id"] if data else None

    async def edit(self, message_id: int, text: str, buttons: Buttons | None = None) -> None:
        payload: dict = {"chat_id": self.chat_id, "message_id": message_id, "text": self._fit(text),
                         "parse_mode": "HTML", "disable_web_page_preview": True}
        if buttons:
            payload["reply_markup"] = self._markup(buttons)
        await self._call("editMessageText", payload)

    # ------------------------------------------------------------ plumbing
    async def send_file(self, path, caption: str = "") -> int | None:
        try:
            data = await asyncio.to_thread(Path(path).read_bytes)
            r = await self.http.post(f"{self.api}/sendDocument",
                                     data={"chat_id": str(self.chat_id), "caption": caption},
                                     files={"document": (Path(path).name, data)}, timeout=300)
            res = r.json()
        except Exception as e:  # noqa: BLE001
            log.warning("telegram sendDocument failed: %s", self._redact(e))
            return None
        if not res.get("ok"):
            log.warning("telegram sendDocument error: %s", res.get("description"))
            return None
        return res["result"]["message_id"]

    @staticmethod
    def _fit(text: str) -> str:
        if len(text) <= MAX_LEN:
            return text
        # Cut inside <pre> blocks safely by dropping the middle of the text.
        cut = text[: MAX_LEN - 200] + "\n…(truncated)…"
        if cut.count("<pre>") > cut.count("</pre>"):
            cut += "</pre>"
        return cut

    @staticmethod
    def _markup(buttons: Buttons) -> dict:
        return {"inline_keyboard": [[{"text": label, "callback_data": data[:64]} for label, data in row]
                                    for row in buttons]}

    async def _call(self, method: str, payload: dict) -> dict | None:
        try:
            r = await self.http.post(f"{self.api}/{method}", json=payload)
            data = r.json()
        except Exception as e:  # noqa: BLE001
            log.warning("telegram %s failed: %s", method, self._redact(e))
            return None
        if not data.get("ok"):
            desc = data.get("description", "")
            if "message is not modified" not in desc:
                log.warning("telegram %s error: %s", method, desc)
                if "parse" in desc.lower() and payload.get("parse_mode"):
                    # Fallback: send as plain text if our HTML was rejected.
                    payload = {**payload, "parse_mode": None}
                    payload.pop("parse_mode")
                    return await self._call(method, payload)
            return None
        return data["result"] if isinstance(data.get("result"), dict) else {}

    async def run(self) -> None:
        await self._call("setMyCommands", {"commands": [
            {"command": c, "description": d} for c, d in [
                ("status", "Overview"), ("pending", "Waiting for my answer"), ("sessions", "Agent sessions & modes"),
                ("tasks", "Scripts"), ("jobs", "Recent runs"), ("away", "Route questions to phone"),
                ("back", "Route questions to terminal"), ("find", "Search"), ("claude", "Start Claude Code"),
                ("opencode", "Start opencode"), ("unlock", "Unlock with your PIN"), ("lock", "Lock now"),
                ("off", "Turn the hub off"), ("help", "All commands")]]})
        await self._skip_backlog()
        log.info("telegram bot polling")
        while True:
            try:
                r = await self.http.post(f"{self.api}/getUpdates", json={
                    "offset": self._offset, "timeout": 50, "allowed_updates": ["message", "callback_query"]})
                if r.status_code == 409:
                    await self._on_conflict()
                    await asyncio.sleep(30)
                    continue
                updates = r.json().get("result", [])
            except Exception as e:  # noqa: BLE001
                log.warning("getUpdates failed: %s", self._redact(e))
                await asyncio.sleep(5)
                continue
            for u in updates:
                self._offset = u["update_id"] + 1
                asyncio.create_task(self._handle(u))

    async def _skip_backlog(self) -> None:
        """Drop messages sent while the hub was off, so an old /run or /stop
        doesn't fire hours later when the hub is turned on."""
        try:
            r = await self.http.post(f"{self.api}/getUpdates", json={"offset": -1, "timeout": 0})
            old = r.json().get("result", [])
        except Exception as e:  # noqa: BLE001
            log.warning("getUpdates failed: %s", self._redact(e))
            old = []
        if old:
            self._offset = old[-1]["update_id"] + 1
            await self.send("🟢 agent-hub is <b>on</b> and 🔒 locked. Messages sent while it was off were ignored. "
                            "/unlock PIN to act, /help")
        else:
            await self.send("🟢 agent-hub is <b>on</b> and 🔒 locked. /unlock PIN to act, /help")

    async def _on_conflict(self) -> None:
        """409 from getUpdates: another program is polling this bot, so someone else has the token."""
        log.error("another program is polling this Telegram bot (409 Conflict): the bot token may be leaked")
        if not self._conflict_warned:
            self._conflict_warned = True
            self.lock()
            await self.send("🚨 <b>Another program is reading this bot's messages.</b> Your bot token may have leaked.\n"
                            "Revoke it now: @BotFather → /mybots → API Token → Revoke, then run "
                            "<code>ahub setup-telegram</code> on the laptop. The phone is locked.")

    async def _handle(self, u: dict) -> None:
        try:
            if "callback_query" in u:
                await self._on_button(u["callback_query"])
            elif "message" in u:
                await self._on_message(u["message"])
        except Exception as e:  # noqa: BLE001
            log.error("handler error: %s", self._redact(e))
            await self.send(f"⚠️ {esc(self._redact(e))}")

    # ------------------------------------------------------------ incoming
    async def _on_message(self, m: dict) -> None:
        if not self._is_owner(m.get("chat", {}), m.get("from")):
            log.warning("ignored message from chat %s / user %s",
                        m.get("chat", {}).get("id"), (m.get("from") or {}).get("id"))
            return
        text = (m.get("text") or "").strip()
        if not text:
            return
        if text.startswith("/"):
            cmd, _, args = text[1:].partition(" ")
            cmd = cmd.split("@")[0].lower()
            if cmd == "unlock":
                await self._unlock(args.strip(), m.get("message_id"))
                return
            handler = getattr(self, f"cmd_{cmd}", None)
            if handler is None:
                await self.send(f"Unknown command /{esc(cmd)}. Try /help")
                return
            if cmd not in OPEN_COMMANDS and not self.unlocked:
                await self._refuse_locked()
                return
            await handler(args.strip())
            return
        # Plain text goes to an agent as an answer or a new instruction: locked too.
        if not self.unlocked:
            await self._refuse_locked()
            return
        # Plain text: it's an answer. Which request? The one being replied to,
        # or the only pending one that accepts text.
        reply_to = (m.get("reply_to_message") or {}).get("message_id")
        if reply_to:
            rows = self.hub.db.query("SELECT * FROM requests WHERE tg_message_id = ?", [reply_to])
            if rows:
                req = rows[0]
                if req["status"] != "pending":
                    await self.send("That question was already answered.")
                    return
                await self.hub.answer_request(req["id"], text)
                return
        pending = [r for r in self.hub.pending_requests() if r["allow_text"]]
        if len(pending) == 1:
            await self.hub.answer_request(pending[0]["id"], text)
        elif not pending:
            await self.send("Nothing is waiting for an answer. /help for commands.")
        else:
            await self.send(f"{len(pending)} questions are waiting — <b>reply</b> to the one you mean. /pending")

    async def _on_button(self, q: dict) -> None:
        if not self._is_owner(q.get("message", {}).get("chat", {}), q.get("from")):
            await self._call("answerCallbackQuery", {"callback_query_id": q["id"]})
            log.warning("ignored button from user %s", (q.get("from") or {}).get("id"))
            return
        if not self.unlocked:
            await self._call("answerCallbackQuery", {"callback_query_id": q["id"], "show_alert": True,
                                                     "text": "🔒 Locked. Send /unlock PIN first."})
            return
        await self._call("answerCallbackQuery", {"callback_query_id": q["id"]})
        kind, _, rest = q.get("data", "").partition(":")
        if kind == "a":  # answer a request: a:<request_id>:<option index>
            rid, _, idx = rest.partition(":")
            req = await self.hub.answer_option(rid, int(idx))
            if req and req["status"] != "pending" and req.get("answer") is None:
                await self.send("That question is no longer pending.")
        elif kind == "run":
            await self.cmd_run(rest)
        elif kind == "log":
            await self.cmd_log(rest)
        elif kind == "stop":
            await self.cmd_stop(rest)
        elif kind == "m":  # m:<session prefix>:<mode>
            sid, _, mode = rest.partition(":")
            await self.cmd_mode(f"{sid} {mode}")
        elif kind == "msg":  # ask for a prompt to send to this session
            await self.cmd_msg(rest)
        elif kind == "s":  # show one session's mode picker
            await self._session_card(rest)
        elif kind == "away":
            await (self.cmd_away("") if rest == "1" else self.cmd_back(""))

    async def _refuse_locked(self) -> None:
        if self._blocked:
            await self.send("🔒 Locked after too many wrong PINs. Restart the hub on the laptop (<code>ahub off</code>, "
                            "<code>ahub on</code>).")
        else:
            await self.send("🔒 The phone is locked. Send <code>/unlock PIN</code> (set it on the laptop with "
                            "<code>ahub pin</code>).")

    async def _unlock(self, pin: str, message_id: int | None) -> None:
        # Remove the PIN from the chat history straight away (works for incoming messages in private chats).
        if message_id and pin:
            if await self._call("deleteMessage", {"chat_id": self.chat_id, "message_id": message_id}) is None:
                await self.send("⚠️ I couldn't delete your PIN message. Delete it yourself.")
        if self._blocked:
            await self._refuse_locked()
            return
        if not has_pin():
            await self.send("No phone PIN is set, so the phone stays locked. On the laptop run <code>ahub pin</code>.")
            return
        if not pin:
            await self.send("Usage: <code>/unlock PIN</code>")
            return
        if await asyncio.to_thread(check_pin, pin):
            self._failed_pins = 0
            minutes = self.hub.cfg.phone_unlock_minutes
            self._unlocked_until = time.monotonic() + minutes * 60
            self.hub.db.log_event("security", "phone unlocked")
            await self.send(f"🔓 Unlocked for {minutes} min. /lock to lock now.")
            return
        self._failed_pins += 1
        self.hub.db.log_event("security", f"wrong phone PIN ({self._failed_pins})")
        left = self.hub.cfg.phone_pin_attempts - self._failed_pins
        if left > 0:
            await asyncio.sleep(1.5)
            await self.send(f"❌ Wrong PIN. {left} attempt(s) left before the hub turns itself off.")
            return
        self.lock()
        await self.send("🚨 Too many wrong PINs. Turning the hub off. If this wasn't you, secure your Telegram "
                        "account and revoke the bot token in @BotFather.")
        if not await self.hub.turn_off("the phone (too many wrong PINs)"):
            self._blocked = True

    async def cmd_lock(self, args: str) -> None:
        self.lock()
        await self.send("🔒 Locked.")

    def _bypass_refused(self, mode: str | None) -> bool:
        return mode == "bypassPermissions" and not self.hub.cfg.phone_allow_bypass

    # ------------------------------------------------------------ commands
    async def cmd_start(self, args: str) -> None:
        await self.send(HELP)

    async def cmd_help(self, args: str) -> None:
        await self.send(HELP)

    async def cmd_status(self, args: str) -> None:
        st = self.hub.status()
        lines = [("🏃 <b>AWAY</b> — questions come to the phone" if st["away"]
                  else "🏠 <b>At computer</b> — questions go to the terminal")]
        if st["running_jobs"]:
            lines.append("\n<b>Running</b>")
            for j in st["running_jobs"]:
                prog = f" {j['progress']:.0f}%" if j.get("progress") is not None else ""
                ptxt = f" — {esc(j['progress_text'])}" if j.get("progress_text") else ""
                lines.append(f"▶️ <code>{j['id']}</code> {esc(j['task'])} {ago(j['started'])}{prog}{ptxt}")
        if st["sessions"]:
            lines.append("\n<b>Agents</b>")
            for s in st["sessions"][:8]:
                lines.append(self._session_line(s))
        if st["pending"]:
            lines.append(f"\n❓ <b>{len(st['pending'])} waiting for you</b> — /pending")
        if st["task_errors"]:
            lines.append(f"\n⚠️ {len(st['task_errors'])} broken task file(s) — /reload to see")
        toggle = [("🏠 I'm back", "away:0")] if st["away"] else [("🏃 I'm away", "away:1")]
        await self.send("\n".join(lines), [toggle + [("🤖 Sessions", "s:"), ("🧰 Tasks", "run:")]])

    async def cmd_away(self, args: str) -> None:
        self.hub.set_away(True)
        await self.send("🏃 Away mode ON. Agent questions, permissions and “finished” messages now come here.")

    async def cmd_back(self, args: str) -> None:
        self.hub.set_away(False)
        await self.send("🏠 Away mode OFF. Agents ask in the terminal again (sessions set to route=phone still come here).")

    async def cmd_pending(self, args: str) -> None:
        pending = self.hub.pending_requests()
        if not pending:
            await self.send("Nothing is waiting for you. ✨")
            return
        for req in pending:
            s = self.hub.db.get("sessions", req["session_id"]) if req.get("session_id") else None
            header = self.hub._session_header(s) if s else None
            msg_id = await self.send(self.hub.render_request(req, header), self.hub._request_buttons(req))
            if msg_id:  # replies to the re-sent copy count too
                self.hub.db.update("requests", req["id"], tg_message_id=msg_id)

    # ---- scripts
    async def cmd_tasks(self, args: str) -> None:
        tasks = sorted(self.hub.tasks.values(), key=lambda t: t.id)
        if not tasks:
            await self.send("No tasks yet. Add YAML files to &lt;project&gt;/.hub/tasks/ and list the project in "
                            "config.yaml. See README → Writing tasks.")
            return
        lines, buttons, row = ["<b>Tasks</b> (tap to run with defaults, or /run name key=value)"], [], []
        for t in tasks:
            flags = (" 🔐" if t.confirm else "") + (f" ⏰{t.schedule}" if t.schedule else "")
            lines.append(f"• <b>{esc(t.id)}</b>{flags} — {esc(t.description.splitlines()[0][:100])}")
            if t.args:
                lines.append(f"   <code>/run {esc(t.usage())}</code>")
            row.append((t.name, f"run:{t.id}"))
            if len(row) == 2:
                buttons.append(row)
                row = []
        if row:
            buttons.append(row)
        await self.send("\n".join(lines), buttons[:20])

    async def cmd_run(self, args: str) -> None:
        if not args:
            await self.cmd_tasks("")
            return
        parts = shlex.split(args)
        values = {}
        for p in parts[1:]:
            if "=" not in p:
                await self.send(f"Arguments must look like key=value (got <code>{esc(p)}</code>)")
                return
            k, _, v = p.partition("=")
            values[k] = v
        try:
            job = await self.hub.run_task(parts[0], values, source="phone")
        except ValueError as e:
            await self.send(f"⚠️ {esc(e)}")
            return
        if job["status"] == "waiting":
            return  # confirmation question was sent

    async def cmd_jobs(self, args: str) -> None:
        jobs = self.hub.recent_jobs(12)
        if not jobs:
            await self.send("No jobs yet.")
            return
        icon = {"running": "▶️", "done": "✅", "failed": "❌", "timeout": "⌛", "stopped": "⏹", "waiting": "🔐",
                "cancelled": "🚫", "lost": "❔", "starting": "…"}
        lines = ["<b>Recent jobs</b>"]
        buttons = []
        for j in jobs:
            prog = f" {j['progress']:.0f}%" if j["status"] == "running" and j.get("progress") is not None else ""
            lines.append(f"{icon.get(j['status'], '·')} <code>{j['id']}</code> {esc(j['task'])}{prog} · {ago(j['started'])} ago")
            if j["status"] == "running":
                buttons.append([(f"📜 {j['id']}", f"log:{j['id']}"), (f"⏹ stop {j['id']}", f"stop:{j['id']}")])
        await self.send("\n".join(lines), buttons or None)

    async def cmd_log(self, args: str) -> None:
        parts = args.split()
        if not parts:
            await self.send("Usage: /log job-id [lines]")
            return
        job, text = self.hub.job_log(parts[0], int(parts[1]) if len(parts) > 1 else None)
        if not job:
            await self.send("No such job.")
            return
        head = f"📜 <b>{esc(job['task'])}</b> <code>{job['id']}</code> — {job['status']}"
        if job.get("progress_text"):
            head += f"\n{esc(job['progress_text'])}"
        await self.send(f"{head}\n<pre>{esc(text[-3500:] or '(no output yet)')}</pre>",
                        [[("🔄 Refresh", f"log:{job['id']}")]] if job["status"] == "running" else None)

    async def cmd_stop(self, args: str) -> None:
        job = await self.hub.stop_job(args.strip())
        await self.send(f"⏹ Stopping {esc(job['task'])}" if job else "No such job.")

    async def cmd_reload(self, args: str) -> None:
        errors = self.hub.reload_tasks()
        msg = f"🔄 Loaded {len(self.hub.tasks)} task(s)."
        if errors:
            msg += "\n⚠️ Problems:\n" + "\n".join(f"• {esc(e)}" for e in errors)
        await self.send(msg)

    # ---- agents
    def _session_line(self, s: dict) -> str:
        mode = s.get("override_mode") or s.get("agent_mode")
        forced = " (set from phone)" if s.get("override_mode") else ""
        route = {"phone": " 📱", "local": " 💻"}.get(s.get("routing"), "")
        status = {"working": "⚙️", "idle": "💤", "active": "·", "ended": "⏹"}.get(s.get("status"), "·")
        return (f"{status} <code>{s['id'][:8]}</code> {esc(self.hub.session_label(s))}{route}\n"
                f"   {policy.MODE_EMOJI.get(mode, '·')} <b>{esc(mode or '?')}</b>{forced} · {ago(s.get('last_seen'))} ago")

    async def cmd_sessions(self, args: str) -> None:
        sessions = self.hub.list_sessions()
        if not sessions:
            await self.send("No agent sessions in the last 24h. (Are the hooks/plugin installed? See README.)")
            return
        lines = ["<b>Agent sessions</b> — tap one to change its mode"]
        buttons = []
        for s in sessions[:10]:
            lines.append(self._session_line(s))
            buttons.append([(f"{s['agent']} {s['id'][:8]} · {self.hub.session_label(s)[-25:]}", f"s:{s['id'][:12]}")])
        await self.send("\n".join(lines), buttons)

    async def _session_card(self, sid: str) -> None:
        if not sid:
            await self.cmd_sessions("")
            return
        s = self.hub.db.find("sessions", sid)
        if not s:
            await self.send("Session not found.")
            return
        last = s.get("last_message") or ""
        text = self._session_line(s) + (f"\n<pre>{esc(last[-1200:])}</pre>" if last else "")
        short = s["id"][:12]
        rows = [[(f"{policy.MODE_EMOJI[m]} {m}", f"m:{short}:{m}") for m in ("default", "acceptEdits", "plan")],
                [(f"{policy.MODE_EMOJI[m]} {m}", f"m:{short}:{m}") for m in ("auto", "bypassPermissions", "pause")
                 if not self._bypass_refused(m)],
                [("↩️ clear (agent decides)", f"m:{short}:clear")],
                [("💬 Message this session", f"msg:{short}")]]
        await self.send(text, rows)

    async def cmd_mode(self, args: str) -> None:
        parts = args.split()
        if len(parts) == 1:
            await self._session_card(parts[0])
            return
        if len(parts) != 2:
            modes = "\n".join(f"• <b>{m}</b> — {d}" for m, d in policy.MODES.items())
            await self.send(f"Usage: /mode session-id mode\n\n{modes}\n• <b>clear</b> — stop overriding")
            return
        try:
            if self._bypass_refused(parts[1]):
                await self.send("⛔ bypassPermissions can't be set from the phone. "
                                "(Allow it with <code>phone_allow_bypass: true</code> in config.yaml.)")
                return
            s = self.hub.set_session_mode(parts[0], parts[1])
        except ValueError as e:
            await self.send(f"⚠️ {esc(e)}")
            return
        if not s:
            await self.send("Session not found.")
            return
        mode = s.get("override_mode")
        note = ""
        if s["agent"] == "claude" and mode in policy.CLAUDE_MODES:
            note = "\n<i>Enforced by the hub now; Claude's own mode switches at its next permission prompt.</i>"
        await self.send(f"{policy.MODE_EMOJI.get(mode, '·')} {esc(self.hub.session_label(s))} → "
                        f"<b>{esc(mode or 'cleared (agent decides)')}</b>{note}")

    async def cmd_route(self, args: str) -> None:
        parts = args.split()
        if len(parts) != 2:
            await self.send("Usage: /route session-id auto|phone|local\n"
                            "auto = phone only while /away · phone = always phone · local = always terminal")
            return
        try:
            s = self.hub.set_session_routing(parts[0], parts[1])
        except ValueError as e:
            await self.send(f"⚠️ {esc(e)}")
            return
        await self.send(f"Routing for {esc(self.hub.session_label(s))} → <b>{parts[1]}</b>" if s else "Session not found.")

    async def cmd_projects(self, args: str) -> None:
        projects = self.hub.cfg.projects
        if not projects:
            await self.send("No projects in config.yaml yet.")
            return
        await self.send("<b>Projects</b>\n" + "\n".join(f"• <b>{esc(n)}</b> — <code>{esc(p)}</code>" for n, p in projects.items()))

    async def _agent(self, agent: str, args: str) -> None:
        project, _, prompt = args.partition(" ")
        if not prompt.strip():
            await self.send(f"Usage: /{agent} project your prompt…\n"
                            f"Optional mode: /{agent} project:plan prompt   (projects: /projects)")
            return
        project, _, mode = project.partition(":")
        if project not in self.hub.cfg.projects:
            await self.send(f"⚠️ Unknown project '{esc(project)}'. From the phone, agents only start in your "
                            "registered projects (/projects).")
            return
        effective = mode or (self.hub.cfg.phone_claude_mode if agent == "claude" else None)
        if self._bypass_refused(effective):
            await self.send("⛔ bypassPermissions can't be used from the phone. "
                            "(Allow it with <code>phone_allow_bypass: true</code> in config.yaml.)")
            return
        try:
            await self.hub.run_agent(agent, project, prompt.strip(), mode or None)
        except ValueError as e:
            await self.send(f"⚠️ {esc(e)}")

    async def cmd_claude(self, args: str) -> None:
        await self._agent("claude", args)

    async def cmd_opencode(self, args: str) -> None:
        await self._agent("opencode", args)

    # ---- find
    async def cmd_get(self, args: str) -> None:
        try:
            p = self.hub.find_file(args)
            await self.hub.send_file(p, str(p))
        except ValueError as e:
            await self.send(f"⚠️ {esc(e)}")

    async def cmd_msg(self, args: str) -> None:
        sid, _, text = args.partition(" ")
        if not sid:
            await self.send("Usage: /msg session-id [prompt] — or tap 💬 under a session in /sessions")
            return
        try:
            if text.strip():
                job = await self.hub.message_session(sid, text.strip())
                await self.send(f"▶️ Sent. Running as job <code>{job['id']}</code> — /log {job['id']}")
            else:
                await self.hub.ask_session_prompt(sid)
        except ValueError as e:
            await self.send(f"⚠️ {esc(e)}")

    async def cmd_off(self, args: str) -> None:
        if not await self.hub.turn_off("the phone"):
            await self.send("This hub can't be turned off from here (not started with <code>ahub on</code>).")

    async def cmd_find(self, args: str) -> None:
        if not args:
            await self.send("Usage: /find text")
            return
        hits = self.hub.find(args)
        await self.send(("<b>Results</b>\n" + "\n".join(hits)) if hits else "No matches.")
