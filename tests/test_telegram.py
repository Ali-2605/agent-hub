"""The Telegram bot with a fake Telegram API: commands, buttons, replies."""
import asyncio
import os
from pathlib import Path

import pytest

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"
CHAT = 42


@pytest.fixture
def bot(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_HUB_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text(f"projects:\n  examples: {EXAMPLES}\n")
    from agenthub.config import load_config
    from agenthub.hub import Hub
    from agenthub.telegram import TelegramBot

    hub = Hub(load_config())
    b = TelegramBot(hub, "TOKEN", CHAT)
    b.calls = []

    async def fake_call(method, payload):
        b.calls.append((method, payload))
        return {"message_id": len(b.calls)}

    b._call = fake_call
    hub.notifier = b
    return b


def msg(text, reply_to=None):
    m = {"chat": {"id": CHAT}, "text": text}
    if reply_to:
        m["reply_to_message"] = {"message_id": reply_to}
    return {"update_id": 1, "message": m}


def texts(b):
    return [p.get("text", "") for m, p in b.calls if m in ("sendMessage", "editMessageText")]


def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro) if False else asyncio.run(coro)


def test_commands_render(bot):
    async def go():
        for cmd in ("/help", "/status", "/tasks", "/jobs", "/sessions", "/pending", "/projects", "/find disk", "/mode"):
            await bot._handle(msg(cmd))
    run(go())
    out = "\n".join(texts(bot))
    assert "agent-hub" in out and "examples/disk-usage" in out and "Nothing is waiting" in out
    assert "⚠️" not in out  # no handler crashed


def test_strangers_are_ignored(bot):
    run(bot._handle({"update_id": 1, "message": {"chat": {"id": 999}, "text": "/status"}}))
    assert bot.calls == []


def test_button_and_reply_answer_requests(bot):
    async def go():
        hub = bot.hub
        r1 = await hub.create_request(kind="question", title="Pick", options=["A", "B"])
        await bot._handle({"update_id": 2, "callback_query": {"id": "x", "data": f"a:{r1['id']}:1",
                                                               "message": {"chat": {"id": CHAT}}}})
        assert hub.db.get("requests", r1["id"])["answer"] == "B"
        r2 = await hub.create_request(kind="question", title="Name?", allow_text=True)
        await bot._handle(msg("Ali", reply_to=hub.db.get("requests", r2["id"])["tg_message_id"]))
        assert hub.db.get("requests", r2["id"])["answer"] == "Ali"
        # with exactly one pending text request, a plain message answers it
        r3 = await hub.create_request(kind="question", title="City?", allow_text=True)
        await bot._handle(msg("Cairo"))
        assert hub.db.get("requests", r3["id"])["answer"] == "Cairo"
    run(go())


def test_away_toggle_and_mode(bot):
    async def go():
        hub = bot.hub
        await bot._handle(msg("/away"))
        assert hub.away
        hub.upsert_session("abc123456789", "claude", cwd="/x/proj", agent_mode="default")
        await bot._handle({"update_id": 3, "callback_query": {"id": "x", "data": "m:abc123456789:plan",
                                                               "message": {"chat": {"id": CHAT}}}})
        assert hub.db.get("sessions", "abc123456789")["override_mode"] == "plan"
        await bot._handle(msg("/back"))
        assert not hub.away
    run(go())


def test_message_session_button(bot, tmp_path):
    hub = bot.hub
    hub.db.insert(
        "sessions", {"id": "abc123-claude", "agent": "claude", "cwd": str(tmp_path), "status": "active"})
    spawned = []

    async def fake_spawn(job_id, extra_env=None):
        spawned.append(hub.db.get("jobs", job_id))

    hub._spawn = fake_spawn

    async def go():
        await bot._handle({"update_id": 3, "callback_query": {"id": "x", "data": "msg:abc123",
                                                               "message": {"chat": {"id": CHAT}}}})
        req = hub.pending_requests()[0]
        assert req["session_id"] == "abc123-claude" and req["allow_text"]
        await bot._handle(msg("write the report", reply_to=req["tg_message_id"]))
        await asyncio.sleep(0.05)
        await bot._handle(msg("/msg abc123 and another one"))
    run(go())
    assert len(spawned) == 2
    argv = spawned[0]["command"]
    assert argv[:3] == ["claude", "-p", "write the report"]
    assert argv[-3:] == ["--resume", "abc123-claude", "--fork-session"]
    assert spawned[0]["cwd"] == str(tmp_path)
    assert spawned[1]["command"][2] == "and another one"
    assert any("Sent. Running as job" in t for t in texts(bot))
