"""Pure decision logic: "should this tool call go to the phone?"

No I/O here, so it is easy to read and to unit-test (tests/test_policy.py).

Two separate ideas:

1. ROUTING: where do questions/permission prompts go?
   - session routing "phone"  -> always the phone
   - session routing "local"  -> always the terminal (hub stays out of the way)
   - session routing "auto"   -> the phone only while you are /away  (default)
   Agent runs started from the phone are always routed to the phone.

2. MODE OVERRIDE: a permission mode you picked from the phone (/mode).
   Claude Code's own mode can only be changed from outside at the moment it
   asks for a permission, so the hub ENFORCES your chosen mode itself on every
   tool call and also syncs Claude's real mode at the next permission prompt.
"""
from __future__ import annotations

# Modes you can pick on the phone. Names match Claude Code's permission modes
# so what you see on the phone means the same thing as in the terminal.
MODES = {
    "default": "ask me before anything that changes things",
    "acceptEdits": "file edits OK, ask me for commands",
    "plan": "read/plan only, no edits",
    "auto": "agent decides, ask me only when it would ask",
    "bypassPermissions": "allow everything",
    "pause": "hold every tool call until I resume",
}
MODE_EMOJI = {
    "default": "🛡", "acceptEdits": "✏️", "plan": "📝", "auto": "🤖",
    "bypassPermissions": "⚡", "dontAsk": "🚫", "pause": "⏸", None: "·",
}
# Modes Claude Code itself understands (pause is hub-only).
CLAUDE_MODES = {"default", "acceptEdits", "plan", "auto", "bypassPermissions"}

CLAUDE_READ_ONLY = {
    "Read", "Glob", "Grep", "LS", "NotebookRead", "WebSearch", "WebFetch", "TodoWrite",
    "Task", "Agent", "ToolSearch", "BashOutput", "TaskOutput", "ListMcpResourcesTool",
    "ReadMcpResourceTool", "ExitPlanMode", "EnterPlanMode", "Skill",
}
CLAUDE_EDIT = {"Edit", "Write", "MultiEdit", "NotebookEdit"}

OPENCODE_READ_ONLY = {"read", "glob", "grep", "list", "todoread", "todowrite", "webfetch",
                      "websearch", "task", "skill", "question", "lsp"}
OPENCODE_EDIT = {"edit", "write", "patch", "multiedit"}


def is_routed(routing: str | None, away: bool, forced: bool = False) -> bool:
    if forced or routing == "phone":
        return True
    if routing == "local":
        return False
    return away


def tool_decision(agent: str, tool: str, override: str | None) -> str:
    """What to do with a tool call given a phone-selected mode.

    Returns one of:
      "pass"   do nothing, the agent's own permission system decides
      "allow"  allow without asking
      "deny"   refuse (the agent is told why)
      "ask"    ask on the phone
      "hold"   wait until the mode changes (pause)
    """
    if override is None or override == "auto":
        return "pass"
    read_only = CLAUDE_READ_ONLY if agent == "claude" else OPENCODE_READ_ONLY
    edit = CLAUDE_EDIT if agent == "claude" else OPENCODE_EDIT
    # The hub's own MCP tools (ask_user, notify_user, ...) are always fine.
    if tool in read_only or tool.startswith("mcp__agent-hub__") or tool.startswith("agent-hub_"):
        return "pass"
    if override == "pause":
        return "hold"
    if override == "bypassPermissions":
        return "allow"
    if override == "plan":
        return "deny" if tool in edit else "ask"
    if override == "acceptEdits":
        return "allow" if tool in edit else "ask"
    return "ask"  # "default"


def describe_tool_call(tool: str, tool_input: dict) -> str:
    """One readable line for the phone: what exactly is the agent trying to do?"""
    ti = tool_input or {}
    for key in ("command", "file_path", "filePath", "path", "url", "pattern", "query", "description", "prompt"):
        if ti.get(key):
            value = str(ti[key])
            return f"{tool}: {value[:1500]}"
    return f"{tool}: {str(ti)[:1500]}"
