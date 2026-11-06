"""Claude Code hook relay:  `ahub hook claude`  (configured in ~/.claude/settings.json).

Claude Code runs this for every hook event and pipes the event JSON on stdin.
We forward it to the hub and print whatever the hub decides. All the logic
lives in Hub.handle_claude_hook — this file is just a relay.

Golden rule: if the hub is down or anything goes wrong, print nothing and
exit 0, so Claude Code behaves exactly as if the hook did not exist.
"""
from __future__ import annotations

import json
import os
import sys
import time

from .client import HubUnavailable, call


def run_claude_hook() -> int:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0
    forced = "true" if os.environ.get("AGENT_HUB_ROUTE") == "phone" else "false"
    try:
        resp = call("POST", f"/api/hook/claude?forced={forced}", payload, timeout=5)
        while resp and resp.get("pending"):
            ticket = resp["pending"]
            try:
                resp = call("GET", f"/api/hook/wait/{ticket}?timeout=50", timeout=60)
            except HubUnavailable:
                time.sleep(3)
                continue
    except Exception as e:  # noqa: BLE001 - never break the agent
        print(f"agent-hub: {e}", file=sys.stderr)
        return 0
    output = (resp or {}).get("output")
    if output:
        print(json.dumps(output))
    return 0
