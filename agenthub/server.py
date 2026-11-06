"""HTTP API (localhost only). Every client — hooks, the opencode plugin, the
CLI, the MCP server, your scripts — talks to the hub through these routes.

Auth: header `X-Hub-Token` must match ~/.config/agent-hub/token.
Interactive docs while the hub runs: http://127.0.0.1:8765/docs
"""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel

from .config import api_token, load_config
from .hub import Hub

log = logging.getLogger("agenthub")


class FileIn(BaseModel):
    path: str
    caption: str = ""
    job_id: str | None = None


class NotifyIn(BaseModel):
    text: str
    title: str | None = None
    job_id: str | None = None


class RequestIn(BaseModel):
    kind: str = "question"
    title: str
    body: str = ""
    options: list[str] = []
    allow_text: bool = False
    source: str = "script"
    job_id: str | None = None


class AnswerIn(BaseModel):
    answer: str


class RunIn(BaseModel):
    task: str
    args: dict[str, str] = {}
    confirmed: bool = False


class AgentRunIn(BaseModel):
    agent: str
    project: str
    prompt: str
    mode: str | None = None


class ProgressIn(BaseModel):
    percent: float | None = None
    text: str = ""


class ModeIn(BaseModel):
    mode: str | None = None


class RouteIn(BaseModel):
    routing: str


class AwayIn(BaseModel):
    away: bool


def create_app(hub: Hub | None = None, with_telegram: bool = True) -> FastAPI:
    state: dict[str, Any] = {}

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        h = hub or Hub(load_config())
        if with_telegram and h.cfg.telegram_token and h.cfg.telegram_chat_id:
            from .telegram import TelegramBot
            bot = TelegramBot(h, h.cfg.telegram_token, h.cfg.telegram_chat_id)
            h.notifier = bot
            state["bot_task"] = asyncio.create_task(bot.run())
        elif with_telegram:
            log.warning("Telegram not configured (TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID in %s/.env). "
                        "Notifications will only be logged.", h.cfg.home)
        await h.start()
        state["hub"] = h
        yield
        await h.stop()
        if t := state.get("bot_task"):
            t.cancel()

    app = FastAPI(title="agent-hub", lifespan=lifespan)
    token = api_token()

    def auth(x_hub_token: str = Header(default="")) -> None:
        if x_hub_token != token:
            raise HTTPException(401, "bad or missing X-Hub-Token")

    def H() -> Hub:
        return state["hub"]

    A = [Depends(auth)]

    @app.get("/api/health")
    async def health():
        return {"ok": True}

    @app.post("/api/shutdown", dependencies=A)
    async def shutdown():
        if not await H().turn_off("the laptop"):
            raise HTTPException(503, "this hub was not started with `ahub serve`")
        return {"ok": True}

    @app.get("/api/status", dependencies=A)
    async def status():
        return H().status()

    @app.post("/api/away", dependencies=A)
    async def away(body: AwayIn):
        H().set_away(body.away)
        return {"away": body.away}

    @app.post("/api/files", dependencies=A)
    async def send_file(body: FileIn):
        try:
            p = await H().send_file(body.path, body.caption, body.job_id)
        except ValueError as e:
            raise HTTPException(400, str(e))
        return {"ok": True, "path": str(p)}

    @app.post("/api/notify", dependencies=A)
    async def notify(body: NotifyIn):
        await H().notify(body.text, body.title, body.job_id)
        return {"ok": True}

    # ---- requests (questions / approvals)
    @app.post("/api/requests", dependencies=A)
    async def create_request(body: RequestIn):
        return await H().create_request(kind=body.kind, title=body.title, body=body.body, options=body.options,
                                        allow_text=body.allow_text, source=body.source, job_id=body.job_id)

    @app.get("/api/requests", dependencies=A)
    async def list_requests(status: str = "pending", limit: int = 50):
        return H().db.query("SELECT * FROM requests WHERE status = ? ORDER BY created DESC LIMIT ?", [status, limit])

    @app.get("/api/requests/{rid}/wait", dependencies=A)
    async def wait_request(rid: str, timeout: float = 50):
        req = await H().wait_request(rid, min(timeout, 55))
        if not req:
            raise HTTPException(404, "no such request")
        return req

    @app.post("/api/requests/{rid}/answer", dependencies=A)
    async def answer_request(rid: str, body: AnswerIn):
        req = await H().answer_request(rid, body.answer)
        if not req:
            raise HTTPException(404, "no such request")
        return req

    # ---- tasks & jobs
    @app.get("/api/tasks", dependencies=A)
    async def tasks():
        return [{"id": t.id, "name": t.name, "project": t.project, "description": t.description,
                 "usage": t.usage(), "confirm": t.confirm, "schedule": t.schedule, "tags": t.tags,
                 "args": [a.__dict__ for a in t.args], "examples": t.examples}
                for t in sorted(H().tasks.values(), key=lambda t: t.id)]

    @app.post("/api/tasks/reload", dependencies=A)
    async def reload_tasks():
        return {"tasks": len(H().tasks), "errors": H().reload_tasks()}

    @app.post("/api/run", dependencies=A)
    async def run(body: RunIn):
        try:
            return await H().run_task(body.task, body.args, source="api", confirmed=body.confirmed)
        except ValueError as e:
            raise HTTPException(400, str(e))

    @app.post("/api/agents/run", dependencies=A)
    async def run_agent(body: AgentRunIn):
        try:
            return await H().run_agent(body.agent, body.project, body.prompt, body.mode)
        except ValueError as e:
            raise HTTPException(400, str(e))

    @app.get("/api/jobs", dependencies=A)
    async def jobs(limit: int = 20):
        return H().recent_jobs(limit)

    @app.get("/api/jobs/{jid}", dependencies=A)
    async def job(jid: str):
        j = H().db.find("jobs", jid)
        if not j:
            raise HTTPException(404, "no such job")
        return j

    @app.get("/api/jobs/{jid}/log", dependencies=A)
    async def job_log(jid: str, lines: int = 100):
        j, text = H().job_log(jid, lines)
        if not j:
            raise HTTPException(404, "no such job")
        return {"job": j, "log": text}

    @app.post("/api/jobs/{jid}/stop", dependencies=A)
    async def stop_job(jid: str):
        j = await H().stop_job(jid)
        if not j:
            raise HTTPException(404, "no such job")
        return j

    @app.post("/api/jobs/{jid}/progress", dependencies=A)
    async def progress(jid: str, body: ProgressIn):
        H().set_progress(jid, body.percent, body.text)
        return {"ok": True}

    # ---- agent sessions
    @app.get("/api/sessions", dependencies=A)
    async def sessions(all: bool = False):
        return H().list_sessions(include_ended=all)

    @app.post("/api/sessions/{sid}/mode", dependencies=A)
    async def set_mode(sid: str, body: ModeIn):
        try:
            s = H().set_session_mode(sid, body.mode)
        except ValueError as e:
            raise HTTPException(400, str(e))
        if not s:
            raise HTTPException(404, "no such session")
        return s

    @app.post("/api/sessions/{sid}/route", dependencies=A)
    async def set_route(sid: str, body: RouteIn):
        try:
            s = H().set_session_routing(sid, body.routing)
        except ValueError as e:
            raise HTTPException(400, str(e))
        if not s:
            raise HTTPException(404, "no such session")
        return s

    # ---- hooks (Claude Code hook script / opencode plugin)
    @app.post("/api/hook/claude", dependencies=A)
    async def hook_claude(payload: dict, forced: bool = False):
        return await H().handle_claude_hook(payload, forced)

    @app.post("/api/hook/opencode", dependencies=A)
    async def hook_opencode(payload: dict, forced: bool = False):
        return await H().handle_opencode_hook(payload, forced)

    @app.get("/api/hook/wait/{ticket}", dependencies=A)
    async def hook_wait(ticket: str, timeout: float = 50):
        r = await H().wait_ticket(ticket, min(timeout, 55))
        if r is None:
            raise HTTPException(404, "unknown ticket (hub restarted?)")
        return r

    @app.get("/api/find", dependencies=A)
    async def find(q: str, limit: int = 20):
        return {"results": H().find(q, limit)}

    return app
