"""Start password and on/off: the real `ahub on` / `ahub off` against a temp home."""
import asyncio
import os
import socket
import subprocess
import sys
import urllib.request
from pathlib import Path

AHUB = str(Path(sys.executable).parent / "ahub")


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def test_password_hash(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_HUB_HOME", str(tmp_path))
    from agenthub import lock
    assert not lock.has_password() and not lock.check_password("x")
    lock.set_password("correct horse")
    assert lock.check_password("correct horse")
    assert not lock.check_password("wrong")
    stored = (tmp_path / "password").read_text()
    assert "correct horse" not in stored
    assert oct((tmp_path / "password").stat().st_mode)[-3:] == "600"


def test_on_off_needs_password(tmp_path):
    port = free_port()
    env = {**os.environ, "AGENT_HUB_HOME": str(tmp_path), "AGENT_HUB_PORT": str(port)}
    (tmp_path / "config.yaml").write_text("projects: {}\n")

    def ahub(*args, stdin=""):
        return subprocess.run([AHUB, *args], input=stdin, env=env, capture_output=True, text=True, timeout=30)

    def up():
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=1)
            return True
        except OSError:
            return False

    r = ahub("on", stdin="anything\n")
    assert r.returncode != 0 and "ahub password" in r.stderr and not up()

    env_home = dict(env)
    subprocess.run([sys.executable, "-c", "from agenthub.lock import set_password; set_password('s3cret!')"],
                   env=env_home, check=True)
    r = ahub("on", stdin="wrong\n")
    assert r.returncode != 0 and "wrong password" in r.stderr and not up()
    r = ahub("serve", stdin="wrong\n")  # the foreground path is gated too
    assert r.returncode != 0 and not up()

    try:
        r = ahub("on", stdin="s3cret!\n")
        assert r.returncode == 0, r.stderr
        assert up() and (tmp_path / "hub.pid").exists()
        assert "already on" in ahub("on").stdout
    finally:
        r = ahub("off")
    assert r.returncode == 0, r.stderr
    assert not up() and not (tmp_path / "hub.pid").exists()
    assert "already off" in ahub("off").stdout


def test_phone_off(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_HUB_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text("projects: {}\n")
    from agenthub.config import load_config
    from agenthub.hub import Hub
    from agenthub.telegram import TelegramBot

    hub = Hub(load_config())
    bot = TelegramBot(hub, "TOKEN", 42)
    sent = []

    async def fake_call(method, payload):
        sent.append(payload.get("text", ""))
        return {"message_id": 1}

    bot._call = fake_call
    hub.notifier = bot
    stopped = []

    async def go():
        await bot._on_message({"chat": {"id": 42, "type": "private"}, "from": {"id": 42}, "text": "/off"})   # not started by `ahub serve`
        hub.on_shutdown = lambda: stopped.append(True)
        await bot._on_message({"chat": {"id": 7, "type": "private"}, "from": {"id": 7}, "text": "/off"})    # stranger: ignored
        await asyncio.sleep(0.4)
        assert not stopped
        await bot._on_message({"chat": {"id": 42, "type": "private"}, "from": {"id": 42}, "text": "/off"})
        await asyncio.sleep(0.4)

    asyncio.run(go())
    assert "can't be turned off" in sent[0]
    assert stopped == [True] and "is <b>off</b>" in sent[-1]
