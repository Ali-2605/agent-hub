"""Phone lock, owner checks, bypass guard, secret files: what protects you if your Telegram is compromised."""
import asyncio
import os
import stat

import pytest

from test_telegram import CHAT, PIN, bot, msg, texts  # noqa: F401  (bot is a fixture)


def run(coro):
    return asyncio.run(coro)


def locked(b):
    b.lock()
    return b


def test_starts_locked_and_refuses_commands(bot):
    locked(bot)
    run(bot._handle(msg("/run examples/disk-usage")))
    run(bot._handle(msg("/claude examples do something")))
    run(bot._handle(msg("/get examples README.md")))
    out = "\n".join(texts(bot))
    assert out.count("phone is locked") == 3
    assert not bot.hub.db.query("SELECT * FROM jobs")  # nothing ran


def test_read_only_commands_and_off_work_while_locked(bot):
    locked(bot)
    run(bot._handle(msg("/status")))
    run(bot._handle(msg("/help")))
    assert all("phone is locked" not in t for t in texts(bot))


def test_text_reply_to_agent_is_refused_while_locked(bot):
    async def go():
        task = asyncio.create_task(bot.hub.ask(kind="question", title="Reply to continue?", options=[], allow_text=True))
        await asyncio.sleep(0.05)
        locked(bot)
        await bot._handle(msg("rm -rf everything please"))
        assert not task.done()
        task.cancel()
    run(go())
    assert any("phone is locked" in t for t in texts(bot))


def test_buttons_refused_while_locked(bot):
    locked(bot)
    run(bot._handle({"update_id": 2, "callback_query": {"id": "x", "data": "run:examples/disk-usage",
                                                        "message": {"chat": {"id": CHAT, "type": "private"}},
                                                        "from": {"id": CHAT}}}))
    alerts = [p for m, p in bot.calls if m == "answerCallbackQuery"]
    assert alerts and alerts[0].get("show_alert") and "Locked" in alerts[0]["text"]
    assert not bot.hub.db.query("SELECT * FROM jobs")


def test_unlock_with_pin_deletes_the_message_and_expires(bot):
    locked(bot)
    run(bot._handle(msg(f"/unlock {PIN}")))
    assert bot.unlocked
    assert ("deleteMessage", {"chat_id": CHAT, "message_id": 500}) in bot.calls
    assert PIN not in "\n".join(texts(bot))
    events = bot.hub.db.query("SELECT * FROM events")
    assert all(PIN not in str(e) for e in events)
    bot._unlocked_until = 0.0  # window expired
    assert not bot.unlocked
    run(bot._handle(msg("/lock")))
    assert not bot.unlocked


def test_wrong_pins_turn_the_hub_off(bot, monkeypatch):
    locked(bot)
    monkeypatch.setattr(asyncio, "sleep", lambda *_: _done())
    turned_off = []

    async def fake_off(source):
        turned_off.append(source)
        return True
    bot.hub.turn_off = fake_off
    for _ in range(bot.hub.cfg.phone_pin_attempts):
        run(bot._handle(msg("/unlock 000000")))
    assert turned_off and "wrong PIN" in turned_off[0]
    assert not bot.unlocked
    assert any("Too many wrong PINs" in t for t in texts(bot))


def test_wrong_pins_block_when_hub_cannot_turn_off(bot, monkeypatch):
    locked(bot)
    monkeypatch.setattr(asyncio, "sleep", lambda *_: _done())

    async def cannot_off(source):
        return False
    bot.hub.turn_off = cannot_off
    for _ in range(bot.hub.cfg.phone_pin_attempts):
        run(bot._handle(msg("/unlock 000000")))
    run(bot._handle(msg(f"/unlock {PIN}")))
    assert not bot.unlocked  # blocked until restarted on the laptop


async def _done():
    return None


def test_no_pin_means_always_locked(bot, tmp_path):
    (tmp_path / "phone_pin").unlink()
    locked(bot)
    run(bot._handle(msg("/unlock anything")))
    assert not bot.unlocked
    assert any("No phone PIN" in t for t in texts(bot))


def test_other_users_in_a_group_are_ignored(bot):
    run(bot._handle({"update_id": 1, "message": {"chat": {"id": CHAT, "type": "group"},
                                                 "from": {"id": 777}, "text": "/status"}}))
    run(bot._handle({"update_id": 1, "message": {"chat": {"id": CHAT, "type": "private"},
                                                 "from": {"id": 777}, "text": "/status"}}))
    assert texts(bot) == []


def test_bypass_refused_from_phone(bot):
    bot.hub.db.insert("sessions", {"id": "abc123456789", "agent": "claude", "cwd": "/tmp", "routing": "auto"})
    run(bot._handle(msg("/mode abc123456789 bypassPermissions")))
    run(bot._handle(msg("/claude examples:bypassPermissions delete it all")))
    out = "\n".join(texts(bot))
    assert out.count("bypassPermissions can't") == 2
    s = bot.hub.db.find("sessions", "abc123456789")
    assert s.get("override_mode") != "bypassPermissions"


def test_agents_only_start_in_registered_projects(bot):
    run(bot._handle(msg("/claude /etc read the shadow file")))
    assert any("Unknown project" in t for t in texts(bot))


def test_bot_token_is_redacted():
    from agenthub.telegram import TelegramBot
    b = TelegramBot.__new__(TelegramBot)
    b._token = "123456:SECRET"
    assert "SECRET" not in b._redact("Client error for url https://api.telegram.org/bot123456:SECRET/getUpdates")


def test_secret_files_are_never_sent(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    from agenthub.hub import check_sendable
    for rel in [".config/gh/hosts.yml", ".git-credentials", "proj/id_ed25519", "proj/prod.tfstate", "proj/.env.local",
                ".kube/config", "proj/cert.p12"]:
        f = tmp_path / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("x")
        with pytest.raises(ValueError):
            check_sendable(f)
    ok = tmp_path / "proj" / "id_ed25519.pub"
    ok.write_text("x")
    assert check_sendable(ok) == ok.resolve()


def test_secret_files_are_private(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_HUB_HOME", str(tmp_path / "home"))
    from agenthub import config, lock
    monkeypatch.setattr(lock, "ITERATIONS", 1000)
    config.api_token()
    lock.set_password("password1")
    lock.set_pin("123456")
    home = tmp_path / "home"
    assert stat.S_IMODE(os.stat(home).st_mode) == 0o700
    for name in ("token", "password", "phone_pin"):
        assert stat.S_IMODE(os.stat(home / name).st_mode) == 0o600
    assert lock.check_pin("123456") and not lock.check_pin("654321")
