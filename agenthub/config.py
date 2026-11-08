"""Configuration: where things live and what the user configured.

Everything the hub stores lives in one folder (the "hub home"):

    ~/.config/agent-hub/          (override with AGENT_HUB_HOME)
        config.yaml               your settings (projects, timeouts, ...)
        .env                      secrets: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
        token                     random API token, created automatically
        password / phone_pin      salted PBKDF2 hashes (laptop start password, phone PIN)
        hub.db                    SQLite: jobs, requests, sessions, settings
        logs/<job-id>.log         output of every job

This module is stdlib-only on purpose: hooks import it and must start fast.
"""
from __future__ import annotations

import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path


def hub_home() -> Path:
    return Path(os.environ.get("AGENT_HUB_HOME", Path.home() / ".config" / "agent-hub")).expanduser()


def ensure_home() -> Path:
    """Create the hub home, readable only by you (it holds secrets)."""
    home = hub_home()
    home.mkdir(parents=True, exist_ok=True)
    try:
        home.chmod(0o700)
    except OSError:
        pass
    return home


def write_private(path: Path, text: str) -> None:
    """Write a secret file that is never readable by other users, not even briefly."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)
    os.chmod(path, 0o600)  # in case the file already existed with wider permissions


def load_env_file(path: Path) -> dict[str, str]:
    """Minimal KEY=VALUE parser (no dependency on python-dotenv)."""
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        out[key.strip()] = value.strip().strip('"').strip("'")
    return out


def api_token() -> str:
    """Shared secret between the hub and its local clients (hooks, CLI, MCP)."""
    path = ensure_home() / "token"
    if not path.exists():
        write_private(path, secrets.token_urlsafe(32))
    return path.read_text().strip()


def hub_url() -> str:
    return os.environ.get("AGENT_HUB_URL", f"http://127.0.0.1:{os.environ.get('AGENT_HUB_PORT', '8765')}")


@dataclass
class Config:
    home: Path
    port: int = 8765
    telegram_token: str = ""
    telegram_chat_id: int = 0
    # name -> folder. Each folder may contain .hub/tasks/*.yaml
    projects: dict[str, Path] = field(default_factory=dict)
    # How long a Stop hook waits for a phone reply before letting the agent stop.
    stop_wait_minutes: int = 30
    # How long a permission request / question waits for you. 0 = forever.
    request_timeout_minutes: int = 0
    # What to answer when a request times out: "deny" or "allow".
    on_timeout: str = "deny"
    # Default Claude permission mode for agent runs started from the phone.
    phone_claude_mode: str = "default"
    log_tail_lines: int = 40
    opencode_model: str = ""
    # Phone lock: commands that act need `/unlock <PIN>` first; the unlock lasts this long.
    phone_unlock_minutes: int = 15
    # Wrong PINs in a row before the hub turns itself off.
    phone_pin_attempts: int = 5
    # Allow bypassPermissions from the phone (/mode, /claude project:bypassPermissions). Off by default.
    phone_allow_bypass: bool = False

    @property
    def db_path(self) -> Path:
        return self.home / "hub.db"

    @property
    def logs_dir(self) -> Path:
        return self.home / "logs"


def load_config() -> Config:
    import yaml  # only the server needs yaml; hooks never call load_config

    home = ensure_home()
    raw: dict = {}
    cfg_file = home / "config.yaml"
    if cfg_file.exists():
        raw = yaml.safe_load(cfg_file.read_text()) or {}

    env = {**load_env_file(home / ".env"), **os.environ}
    projects = {
        str(name): Path(os.path.expanduser(str(path))).resolve()
        for name, path in (raw.get("projects") or {}).items()
    }
    cfg = Config(
        home=home,
        port=int(raw.get("port", env.get("AGENT_HUB_PORT", 8765))),
        telegram_token=env.get("TELEGRAM_BOT_TOKEN", ""),
        telegram_chat_id=int(env.get("TELEGRAM_CHAT_ID", "0") or 0),
        projects=projects,
        stop_wait_minutes=int(raw.get("stop_wait_minutes", 30)),
        request_timeout_minutes=int(raw.get("request_timeout_minutes", 0)),
        on_timeout=str(raw.get("on_timeout", "deny")),
        phone_claude_mode=str(raw.get("phone_claude_mode", "default")),
        log_tail_lines=int(raw.get("log_tail_lines", 40)),
        opencode_model=str(raw.get("opencode_model") or ""),
        phone_unlock_minutes=max(1, int(raw.get("phone_unlock_minutes", 15))),
        phone_pin_attempts=max(1, int(raw.get("phone_pin_attempts", 5))),
        phone_allow_bypass=bool(raw.get("phone_allow_bypass", False)),
    )
    cfg.logs_dir.mkdir(parents=True, exist_ok=True)
    return cfg
