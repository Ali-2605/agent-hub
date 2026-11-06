"""Configuration: where things live and what the user configured.

Everything the hub stores lives in one folder (the "hub home"):

    ~/.config/agent-hub/          (override with AGENT_HUB_HOME)
        config.yaml               your settings (projects, timeouts, ...)
        .env                      secrets: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
        token                     random API token, created automatically
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
    path = hub_home() / "token"
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(secrets.token_urlsafe(24))
        path.chmod(0o600)
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

    @property
    def db_path(self) -> Path:
        return self.home / "hub.db"

    @property
    def logs_dir(self) -> Path:
        return self.home / "logs"


def load_config() -> Config:
    import yaml  # only the server needs yaml; hooks never call load_config

    home = hub_home()
    home.mkdir(parents=True, exist_ok=True)
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
    )
    cfg.logs_dir.mkdir(parents=True, exist_ok=True)
    return cfg
