"""`ahub` command line. Run `ahub -h` or `ahub <command> -h`.

Imports are done inside each command so that `ahub hook claude` (which runs
on every Claude Code tool call) starts in milliseconds.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
INTEGRATIONS = REPO / "integrations"


def ahub_bin() -> str:
    exe = Path(sys.executable).parent / "ahub"
    return str(exe) if exe.exists() else (shutil.which("ahub") or "ahub")


def die(msg: str) -> None:
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(1)


def kv_args(pairs: list[str]) -> dict[str, str]:
    out = {}
    for p in pairs:
        if "=" not in p:
            die(f"arguments must be key=value (got '{p}')")
        k, _, v = p.partition("=")
        out[k] = v
    return out


# --------------------------------------------------------------------- server
def _read_password(prompt: str = "agent-hub password: ") -> str:
    """From the keyboard, or from stdin when `ahub on` starts the server."""
    import getpass
    if sys.stdin is not None and sys.stdin.isatty():
        return getpass.getpass(prompt)
    return (sys.stdin.readline() if sys.stdin else "").rstrip("\n")


def _require_password() -> str:
    from .lock import check_password, has_password
    if not has_password():
        die("no start password set yet. Run on the laptop: ahub password")
    pw = _read_password()
    if not check_password(pw):
        time.sleep(1.5)
        die("wrong password")
    return pw


def cmd_serve(a: argparse.Namespace) -> None:
    import logging

    import uvicorn

    from .config import hub_home, load_config
    from .hub import Hub
    from .server import create_app

    _require_password()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = load_config()
    hub = Hub(cfg)
    server = uvicorn.Server(uvicorn.Config(create_app(hub), host="127.0.0.1", port=a.port or cfg.port,
                                           log_level="warning"))
    hub.on_shutdown = lambda: setattr(server, "should_exit", True)
    pid = hub_home() / "hub.pid"
    pid.write_text(str(os.getpid()))
    try:
        server.run()
    finally:
        pid.unlink(missing_ok=True)


def cmd_on(a: argparse.Namespace) -> None:
    from . import client as c
    from .config import hub_home
    if c.is_up():
        print("agent-hub is already on.")
        return
    pw = _require_password()
    log_path = hub_home() / "hub.log"
    with open(log_path, "ab") as log_file:
        proc = subprocess.Popen([ahub_bin(), "serve"], stdin=subprocess.PIPE, stdout=log_file,
                                stderr=subprocess.STDOUT, start_new_session=True)
    proc.stdin.write((pw + "\n").encode())
    proc.stdin.close()
    for _ in range(40):
        if c.is_up():
            print("🟢 agent-hub is on. Turn it off with `ahub off` or /off on the phone.")
            return
        if proc.poll() is not None:
            break
        time.sleep(0.25)
    tail = log_path.read_text(errors="replace").splitlines()[-15:]
    die("the hub did not start. Last lines of " + str(log_path) + ":\n" + "\n".join(tail))


def cmd_off(a: argparse.Namespace) -> None:
    import signal

    from . import client as c
    from .config import hub_home
    pid_path = hub_home() / "hub.pid"
    if not c.is_up():
        pid_path.unlink(missing_ok=True)
        print("agent-hub is already off.")
        return
    try:
        c.call("POST", "/api/shutdown")
    except Exception:  # noqa: BLE001  (e.g. started some other way) -> signal it
        if pid_path.exists():
            os.kill(int(pid_path.read_text()), signal.SIGTERM)
    for _ in range(40):
        if not c.is_up():
            print("🔴 agent-hub is off. Agents ask in the terminal again.")
            return
        time.sleep(0.25)
    die("the hub is still answering; stop it manually (pkill -f 'ahub serve')")


def cmd_password(a: argparse.Namespace) -> None:
    import getpass

    from .lock import check_password, has_password, set_password
    if has_password() and not check_password(getpass.getpass("Current password: ")):
        time.sleep(1.5)
        die("wrong password")
    new = getpass.getpass("New password: ")
    if len(new) < 6:
        die("use at least 6 characters")
    if new != getpass.getpass("Repeat it: "):
        die("passwords don't match")
    set_password(new)
    print("Password saved. Start the hub with: ahub on")


def cmd_hook(a: argparse.Namespace) -> None:
    if a.agent != "claude":
        die("only 'claude' uses the hook command (opencode uses the plugin)")
    from .hooks import run_claude_hook
    sys.exit(run_claude_hook())


def cmd_mcp(a: argparse.Namespace) -> None:
    from .mcp_server import main
    main()


# --------------------------------------------------------------------- client commands
def client():
    from . import client as c
    return c


def cmd_status(a):
    st = client().call("GET", "/api/status")
    if a.json:
        print(json.dumps(st, indent=2))
        return
    print("AWAY (phone)" if st["away"] else "at computer (terminal)")
    for j in st["running_jobs"]:
        print(f"  running  {j['id']}  {j['task']}  {j.get('progress_text') or ''}")
    for s in st["sessions"]:
        mode = s["override_mode"] or s["agent_mode"]
        print(f"  session  {s['id'][:8]}  {s['agent']:<8} {mode or '?':<17} {s['cwd']}")
    print(f"  {len(st['pending'])} pending question(s), {st['tasks']} task(s)")
    for e in st["task_errors"]:
        print(f"  ! {e}")


def cmd_notify(a):
    client().call("POST", "/api/notify", {"text": a.text, "title": a.title, "job_id": os.environ.get("AGENT_HUB_JOB_ID")})


def cmd_send(a):
    for f in a.files:
        r = client().call("POST", "/api/files", {"path": os.path.abspath(os.path.expanduser(f)),
                                                 "caption": a.caption or "",
                                                 "job_id": os.environ.get("AGENT_HUB_JOB_ID")}, timeout=310)
        print(f"sent {r['path']}")


def cmd_ask(a):
    answer = client().ask(a.question, options=a.option or None, timeout_s=a.timeout or None, default=a.default)
    if answer is None:
        sys.exit(2)
    print(answer)


def cmd_progress(a):
    client().progress(a.percent, a.text or "")


def cmd_run(a):
    c = client()
    job = c.call("POST", "/api/run", {"task": a.task, "args": kv_args(a.args)})
    print(f"job {job['id']} {job['status']}")
    if not a.wait:
        return
    while job["status"] in ("running", "starting", "waiting"):
        time.sleep(1)
        job = c.call("GET", f"/api/jobs/{job['id']}")
    print(c.call("GET", f"/api/jobs/{job['id']}/log?lines=200")["log"])
    print(f"-- {job['status']} (exit {job['exit_code']})")
    sys.exit(0 if job["status"] == "done" else 1)


def cmd_jobs(a):
    for j in client().call("GET", f"/api/jobs?limit={a.limit}"):
        print(f"{j['id']}  {j['status']:<9} {j['task']}")


def cmd_log(a):
    r = client().call("GET", f"/api/jobs/{a.job}/log?lines={a.lines}")
    print(r["log"])


def cmd_away(a):
    client().call("POST", "/api/away", {"away": a.cmd == "away"})
    print("away: questions go to the phone" if a.cmd == "away" else "back: questions go to the terminal")


def cmd_mode(a):
    s = client().call("POST", f"/api/sessions/{a.session}/mode", {"mode": a.mode})
    print(f"{s['id'][:8]} -> {s['override_mode'] or 'cleared'}")


def cmd_find(a):
    import html
    import re
    for line in client().call("GET", f"/api/find?q={__import__('urllib.parse').parse.quote(a.query)}")["results"]:
        print(html.unescape(re.sub(r"<[^>]+>", "", line)))


# --------------------------------------------------------------------- tasks & docs
def _load_local_tasks():
    from .config import load_config
    from .tasks import load_tasks
    cfg = load_config()
    return cfg, *load_tasks(cfg.projects)


def cmd_tasks(a):
    from .tasks import catalog_markdown
    cfg, tasks, errors = _load_local_tasks()
    if a.md:
        print(catalog_markdown(tasks))
    else:
        for t in sorted(tasks.values(), key=lambda t: t.id):
            print(f"{t.id:<30} {t.description.splitlines()[0][:80]}")
    for e in errors:
        print(f"! {e}", file=sys.stderr)
    if errors and a.check:
        sys.exit(1)


def cmd_docs(a):
    """Write <project>/.hub/TASKS.md for every project (what agents read)."""
    from .tasks import catalog_markdown
    cfg, tasks, errors = _load_local_tasks()
    for name, root in cfg.projects.items():
        mine = {k: t for k, t in tasks.items() if t.project == name}
        if not (root / ".hub").exists():
            continue
        out = root / ".hub" / "TASKS.md"
        out.write_text(catalog_markdown(mine))
        print(f"wrote {out} ({len(mine)} tasks)")
    for e in errors:
        print(f"! {e}", file=sys.stderr)


TASK_TEMPLATE = """# One file per script. Docs: {repo}/docs/writing-tasks.md
name: hello
description: >
  Says hello. Replace this with ONE clear sentence about what the script does,
  when to use it and whether it is safe to run anytime. Agents read this.
command: python3 -c "print('hello {{who}}')"
args:
  who:
    description: who to greet
    default: world
notify: always        # always | failure | never
# confirm: true       # ask on the phone before running
# timeout: 600        # seconds
# schedule: daily 08:00   # or: every 30m
examples:
  - ahub run hello who=Ali
"""


def cmd_init(a):
    """Make a folder a hub project: create .hub/tasks/ and register it."""
    from .config import hub_home
    root = Path(a.path).expanduser().resolve()
    name = a.name or root.name.lower().replace(" ", "-")
    tasks_dir = root / ".hub" / "tasks"
    tasks_dir.mkdir(parents=True, exist_ok=True)
    example = tasks_dir / "hello.yaml"
    if not any(tasks_dir.iterdir()):
        example.write_text(TASK_TEMPLATE.format(repo=REPO))
        print(f"created {example}")
    cfg_file = hub_home() / "config.yaml"
    if not cfg_file.exists():
        shutil.copy(REPO / "config.example.yaml", cfg_file)
    text = cfg_file.read_text()
    import yaml
    projects = (yaml.safe_load(text) or {}).get("projects") or {}
    if name in projects:
        print(f"project '{name}' already registered")
    else:
        lines = text.splitlines()
        idx = next((i for i, l in enumerate(lines) if l.startswith("projects:")), None)
        entry = f"  {name}: {root}"
        if idx is None:
            lines += ["", "projects:", entry]
        else:
            if lines[idx].strip() != "projects:":  # "projects: {}" style
                lines[idx] = "projects:"
            lines.insert(idx + 1, entry)
        cfg_file.write_text("\n".join(lines) + "\n")
        print(f"registered project '{name}' -> {root} in {cfg_file}")
    print("Next: edit the YAML, then `ahub reload` (or /reload on the phone).")


def cmd_reload(a):
    r = client().call("POST", "/api/tasks/reload")
    print(f"{r['tasks']} task(s) loaded")
    for e in r["errors"]:
        print(f"! {e}")


# --------------------------------------------------------------------- install
def _backup(path: Path) -> None:
    if path.exists():
        bak = path.with_suffix(path.suffix + f".bak-{time.strftime('%Y%m%d-%H%M%S')}")
        shutil.copy(path, bak)
        print(f"backup: {bak}")


def claude_hooks_config(cmd: str) -> dict:
    blocking = {"type": "command", "command": cmd, "timeout": 86400}  # waits for you
    quick = {"type": "command", "command": cmd, "timeout": 10}
    return {
        "PreToolUse": [{"matcher": "*", "hooks": [blocking]}],
        "PermissionRequest": [{"matcher": "*", "hooks": [blocking]}],
        "Stop": [{"hooks": [blocking]}],
        "Notification": [{"hooks": [quick]}],
        "UserPromptSubmit": [{"hooks": [quick]}],
        "SessionStart": [{"hooks": [quick]}],
        "SessionEnd": [{"hooks": [{"type": "command", "command": cmd, "timeout": 2}]}],
    }


def install_claude(a):
    cmd = f"{ahub_bin()} hook claude"
    path = Path(a.settings).expanduser() if a.settings else Path.home() / ".claude" / "settings.json"
    settings = json.loads(path.read_text()) if path.exists() else {}
    hooks = settings.setdefault("hooks", {})
    for event, entries in claude_hooks_config(cmd).items():
        existing = hooks.setdefault(event, [])
        # remove older agent-hub entries, keep everything else the user has
        existing[:] = [e for e in existing if not any("hook claude" in h.get("command", "") and "ahub" in h.get("command", "")
                                                     for h in e.get("hooks", []))]
        existing.extend(entries)
    if a.dry_run:
        print(json.dumps({"hooks": hooks}, indent=2))
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    _backup(path)
    path.write_text(json.dumps(settings, indent=2) + "\n")
    print(f"hooks installed in {path}")
    if shutil.which("claude") and not a.no_mcp:
        subprocess.run(["claude", "mcp", "remove", "--scope", "user", "agent-hub"], capture_output=True)
        r = subprocess.run(["claude", "mcp", "add", "--scope", "user", "agent-hub", "--", ahub_bin(), "mcp"],
                           capture_output=True, text=True)
        print("MCP server registered (user scope)" if r.returncode == 0 else f"MCP registration failed: {r.stderr}")


def install_opencode(a):
    plugin_dir = Path.home() / ".config" / "opencode" / "plugins"
    plugin_dir.mkdir(parents=True, exist_ok=True)
    src = INTEGRATIONS / "opencode" / "agent-hub.js"
    dst = plugin_dir / "agent-hub.js"
    if a.dry_run:
        print(f"would copy {src} -> {dst}")
    else:
        shutil.copy(src, dst)
        print(f"plugin installed: {dst}")
    # Register the MCP server with opencode's own command (it handles opencode.jsonc
    # comments and the 2.x config shape: {"mcp": {"servers": {name: {...}}}}).
    cmd = ["opencode", "mcp", "add", "--global", "agent-hub", "--", ahub_bin(), "mcp"]
    if a.dry_run or not shutil.which("opencode"):
        print("run: " + " ".join(cmd))
        return
    r = subprocess.run(cmd, capture_output=True, text=True)
    out = (r.stdout + r.stderr).strip()
    print(out or "MCP server registered with opencode")
    if r.returncode != 0:
        print("If it already exists that's fine. Otherwise add this to ~/.config/opencode/opencode.json(c):")
        print(json.dumps({"mcp": {"servers": {"agent-hub": {"type": "local", "command": [ahub_bin(), "mcp"]}}}}, indent=2))
    print("Restart opencode (and its background service) so the plugin and MCP server load.")


def install_skill(a):
    src = REPO / "skills" / "agent-hub" / "SKILL.md"
    for base in (Path.home() / ".claude" / "skills", Path.home() / ".config" / "opencode" / "skills"):
        dst = base / "agent-hub" / "SKILL.md"
        if a.dry_run:
            print(f"would copy skill -> {dst}")
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(src, dst)
        print(f"skill installed: {dst}")


def install_service(a):
    from .lock import has_password
    if has_password() and not a.dry_run:
        die("a start password is set, so the hub only starts with `ahub on`. "
            "An auto-start service can't type the password, so it isn't installed.")
    unit = f"""[Unit]
Description=agent-hub (phone control for scripts and coding agents)
After=network-online.target

[Service]
ExecStart={ahub_bin()} serve
Restart=always
RestartSec=3
Environment=PATH={os.environ.get('PATH', '/usr/bin:/bin')}

[Install]
WantedBy=default.target
"""
    path = Path.home() / ".config" / "systemd" / "user" / "agent-hub.service"
    if a.dry_run:
        print(unit)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(unit)
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=False)
    subprocess.run(["systemctl", "--user", "enable", "--now", "agent-hub"], check=False)
    subprocess.run(["loginctl", "enable-linger", os.environ.get("USER", "")], check=False)
    print(f"service installed: {path}\n  logs:    journalctl --user -u agent-hub -f\n  restart: systemctl --user restart agent-hub")


def cmd_install(a):
    targets = ["claude", "opencode", "skill"] if a.target == "all" else [a.target]
    for t in targets:
        print(f"== {t}")
        {"claude": install_claude, "opencode": install_opencode, "skill": install_skill, "service": install_service}[t](a)


# --------------------------------------------------------------------- telegram setup
def cmd_setup_telegram(a):
    import httpx

    from .config import hub_home, load_env_file
    env_path = hub_home() / ".env"
    env = load_env_file(env_path)
    token = a.token or env.get("TELEGRAM_BOT_TOKEN") or input("Paste the bot token from @BotFather: ").strip()
    api = f"https://api.telegram.org/bot{token}"
    me = httpx.get(f"{api}/getMe", timeout=20).json()
    if not me.get("ok"):
        die(f"token rejected by Telegram: {me.get('description')}")
    print(f"Bot OK: @{me['result']['username']}")
    print(f"Now open Telegram on your phone, find @{me['result']['username']} and send it any message (e.g. /start)...")
    chat_id, offset, deadline = None, 0, time.time() + 180
    while time.time() < deadline and not chat_id:
        r = httpx.post(f"{api}/getUpdates", json={"timeout": 25, "offset": offset}, timeout=35).json()
        for u in r.get("result", []):
            offset = u["update_id"] + 1
            msg = u.get("message") or {}
            if msg.get("chat", {}).get("type") == "private":
                chat_id = msg["chat"]["id"]
                print(f"Got it: chat id {chat_id} ({msg['chat'].get('first_name', '')})")
    if not chat_id:
        die("no message received within 3 minutes; run this again")
    httpx.post(f"{api}/getUpdates", json={"offset": offset, "timeout": 0}, timeout=10)  # mark as read
    env.update({"TELEGRAM_BOT_TOKEN": token, "TELEGRAM_CHAT_ID": str(chat_id)})
    env_path.parent.mkdir(parents=True, exist_ok=True)
    env_path.write_text("".join(f"{k}={v}\n" for k, v in env.items()))
    env_path.chmod(0o600)
    httpx.post(f"{api}/sendMessage", json={"chat_id": chat_id, "text": "✅ agent-hub is connected. Send /help."}, timeout=20)
    print(f"Saved to {env_path}. Set a start password with `ahub password`, then: ahub on (or `ahub off` and `ahub on` if it is running).")


def cmd_doctor(a):
    from . import client as c
    from .config import hub_home, hub_url, load_config
    cfg = load_config()
    ok = lambda b: "OK " if b else "!! "  # noqa: E731
    print(f"{ok(True)}hub home: {hub_home()}")
    print(f"{ok(bool(cfg.telegram_token and cfg.telegram_chat_id))}telegram configured (ahub setup-telegram)")
    from .lock import has_password
    print(f"{ok(has_password())}start password set (ahub password)")
    print(f"{ok(c.is_up())}hub on at {hub_url()} (ahub on)")
    print(f"{ok(bool(cfg.projects))}{len(cfg.projects)} project(s) in config.yaml (ahub init <folder>)")
    _, tasks, errors = _load_local_tasks()
    print(f"{ok(not errors)}{len(tasks)} task(s), {len(errors)} broken")
    for e in errors:
        print(f"     {e}")
    settings = Path.home() / ".claude" / "settings.json"
    print(f"{ok(settings.exists() and 'ahub' in settings.read_text())}Claude Code hooks (ahub install claude)")
    print(f"{ok((Path.home() / '.config/opencode/plugins/agent-hub.js').exists())}opencode plugin (ahub install opencode)")


# --------------------------------------------------------------------- parser
def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="ahub", description="agent-hub: control scripts & coding agents from your phone")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("on", help="start the hub in the background (asks for the password)")
    s.set_defaults(fn=cmd_on)

    s = sub.add_parser("off", help="stop the hub (agents go back to asking in the terminal)")
    s.set_defaults(fn=cmd_off)

    s = sub.add_parser("password", help="set or change the start password")
    s.set_defaults(fn=cmd_password)

    s = sub.add_parser("serve", help="run the hub in the foreground (asks for the password)")
    s.add_argument("--port", type=int)
    s.set_defaults(fn=cmd_serve)

    s = sub.add_parser("status", help="overview")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_status)

    s = sub.add_parser("notify", help="send a message to the phone")
    s.add_argument("text")
    s.add_argument("--title")
    s.set_defaults(fn=cmd_notify)

    s = sub.add_parser("send", help="send file(s) to the phone (up to 50 MB each)")
    s.add_argument("files", nargs="+")
    s.add_argument("-c", "--caption")
    s.set_defaults(fn=cmd_send)

    s = sub.add_parser("ask", help="ask on the phone, print the answer (exit 2 if unanswered)")
    s.add_argument("question")
    s.add_argument("-o", "--option", action="append", help="a button (repeat for more)")
    s.add_argument("--timeout", type=float, help="seconds")
    s.add_argument("--default")
    s.set_defaults(fn=cmd_ask)

    s = sub.add_parser("progress", help="update progress of the current job")
    s.add_argument("percent", type=float)
    s.add_argument("text", nargs="?")
    s.set_defaults(fn=cmd_progress)

    s = sub.add_parser("run", help="run a task: ahub run name key=value ...")
    s.add_argument("task")
    s.add_argument("args", nargs="*")
    s.add_argument("-w", "--wait", action="store_true", help="wait and print output")
    s.set_defaults(fn=cmd_run)

    s = sub.add_parser("jobs", help="recent jobs")
    s.add_argument("-n", "--limit", type=int, default=15)
    s.set_defaults(fn=cmd_jobs)

    s = sub.add_parser("log", help="output of a job")
    s.add_argument("job")
    s.add_argument("-n", "--lines", type=int, default=100)
    s.set_defaults(fn=cmd_log)

    for name, helptext in (("away", "send agent questions to the phone"), ("back", "send them to the terminal")):
        s = sub.add_parser(name, help=helptext)
        s.set_defaults(fn=cmd_away)

    s = sub.add_parser("mode", help="override an agent session's mode")
    s.add_argument("session")
    s.add_argument("mode", help="default|acceptEdits|plan|auto|bypassPermissions|pause|clear")
    s.set_defaults(fn=cmd_mode)

    s = sub.add_parser("find", help="search tasks, logs, agent messages")
    s.add_argument("query")
    s.set_defaults(fn=cmd_find)

    s = sub.add_parser("tasks", help="list tasks (reads files directly, hub not needed)")
    s.add_argument("--md", action="store_true", help="print the markdown catalog")
    s.add_argument("--check", action="store_true", help="exit 1 if any task file is broken")
    s.set_defaults(fn=cmd_tasks)

    s = sub.add_parser("docs", help="write .hub/TASKS.md in every project")
    s.set_defaults(fn=cmd_docs)

    s = sub.add_parser("init", help="make a folder a hub project")
    s.add_argument("path", nargs="?", default=".")
    s.add_argument("--name")
    s.set_defaults(fn=cmd_init)

    s = sub.add_parser("reload", help="reload task files in the running hub")
    s.set_defaults(fn=cmd_reload)

    s = sub.add_parser("install", help="connect Claude Code / opencode / skill / systemd service")
    s.add_argument("target", choices=["claude", "opencode", "skill", "service", "all"])
    s.add_argument("--dry-run", action="store_true")
    s.add_argument("--settings", help="Claude settings file (default ~/.claude/settings.json)")
    s.add_argument("--no-mcp", action="store_true", help="don't register the MCP server with Claude Code")
    s.set_defaults(fn=cmd_install)

    s = sub.add_parser("setup-telegram", help="connect your Telegram bot (interactive)")
    s.add_argument("--token")
    s.set_defaults(fn=cmd_setup_telegram)

    s = sub.add_parser("doctor", help="check the setup")
    s.set_defaults(fn=cmd_doctor)

    s = sub.add_parser("hook", help="(internal) Claude Code hook relay")
    s.add_argument("agent")
    s.set_defaults(fn=cmd_hook)

    s = sub.add_parser("mcp", help="(internal) MCP server on stdio")
    s.set_defaults(fn=cmd_mcp)

    a = p.parse_args(argv)
    try:
        a.fn(a)
    except KeyboardInterrupt:
        sys.exit(130)
    except Exception as e:  # noqa: BLE001
        from .client import HubUnavailable
        if isinstance(e, HubUnavailable):
            die(f"{e}\n  turn it on: ahub on")
        raise


if __name__ == "__main__":
    main()
