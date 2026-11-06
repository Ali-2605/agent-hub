from agenthub.policy import is_routed, tool_decision


def test_routing():
    assert is_routed("auto", away=True)
    assert not is_routed("auto", away=False)
    assert is_routed("phone", away=False)
    assert not is_routed("local", away=True)
    assert is_routed("local", away=False, forced=True)


def test_no_override_passes():
    assert tool_decision("claude", "Bash", None) == "pass"
    assert tool_decision("claude", "Bash", "auto") == "pass"


def test_modes_claude():
    assert tool_decision("claude", "Read", "default") == "pass"
    assert tool_decision("claude", "Bash", "default") == "ask"
    assert tool_decision("claude", "Edit", "acceptEdits") == "allow"
    assert tool_decision("claude", "Bash", "acceptEdits") == "ask"
    assert tool_decision("claude", "Write", "plan") == "deny"
    assert tool_decision("claude", "Bash", "plan") == "ask"
    assert tool_decision("claude", "Bash", "bypassPermissions") == "allow"
    assert tool_decision("claude", "Bash", "pause") == "hold"
    assert tool_decision("claude", "mcp__agent-hub__ask_user", "pause") == "pass"


def test_modes_opencode():
    assert tool_decision("opencode", "read", "plan") == "pass"
    assert tool_decision("opencode", "edit", "plan") == "deny"
    assert tool_decision("opencode", "bash", "default") == "ask"
