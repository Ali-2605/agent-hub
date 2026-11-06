// agent-hub plugin for opencode 2.x
// Installed by `ahub install opencode` into ~/.config/opencode/plugins/agent-hub.js
//
// What it does (all decisions are made by the hub; this file only relays):
//   - tool "execute.before"     enforces the mode you picked on the phone (/mode)
//   - permission "evaluate"     when opencode would ask you, asks the phone instead (while routed)
//   - events                    reports sessions; when a session goes idle, sends the agent's
//                               last message to the phone and feeds your reply back as a prompt
//   - mode → agent              picking "plan" on the phone switches the session to the plan
//                               agent, other modes switch back to build
//
// Golden rule: if the hub is down, do nothing (opencode behaves normally).
// Plugin API used: default export { id, setup(ctx) } (opencode 2.x).

import { readFileSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";

const HOME = process.env.AGENT_HUB_HOME || join(homedir(), ".config", "agent-hub");
const URL = process.env.AGENT_HUB_URL || `http://127.0.0.1:${process.env.AGENT_HUB_PORT || "8765"}`;
const FORCED = process.env.AGENT_HUB_ROUTE === "phone";
const JOB = process.env.AGENT_HUB_JOB_ID || null; // set when the hub started this run

function token() {
  try {
    return readFileSync(join(HOME, "token"), "utf8").trim();
  } catch {
    return "";
  }
}

async function call(method, path, body, timeoutMs) {
  const res = await fetch(URL + path, {
    method,
    headers: { "Content-Type": "application/json", "X-Hub-Token": token() },
    body: body ? JSON.stringify(body) : undefined,
    signal: AbortSignal.timeout(timeoutMs),
  });
  if (!res.ok) throw new Error(`agent-hub ${res.status}`);
  return res.json();
}

export default {
  id: "agent-hub",
  setup(ctx) {
    const controller = new AbortController();
    const directory = process.cwd();
    const lastText = new Map(); // sessionID -> { messageID, text }
    const lastIdle = new Map(); // sessionID -> timestamp (dedupe idle events)

    async function switchAgent(sessionID, agent) {
      try {
        await ctx.session.switchAgent({ sessionID, agent });
      } catch (e) {
        console.error("[agent-hub] switchAgent failed:", e?.message || e);
      }
    }

    // Send an event to the hub; if it needs you, poll until there is an answer.
    async function decide(payload) {
      try {
        let resp = await call("POST", `/api/hook/opencode?forced=${FORCED}`, { directory, job: JOB, ...payload }, 3000);
        if (resp && resp.switch_agent && payload.sessionID) await switchAgent(payload.sessionID, resp.switch_agent);
        while (resp && resp.pending) {
          try {
            resp = await call("GET", `/api/hook/wait/${resp.pending}?timeout=50`, null, 60000);
          } catch (e) {
            if (String(e).includes("404")) return null; // hub restarted: behave normally
            await new Promise((r) => setTimeout(r, 3000));
          }
        }
        return resp ? resp.output : null;
      } catch {
        return null; // hub not running
      }
    }

    const disposers = [];

    // 1) Every tool call: enforce the phone-selected mode.
    disposers.push(
      ctx.tool.hook("execute.before", async (a) => {
        const out = await decide({
          event: "tool.before", sessionID: a.sessionID, agent: a.agent, tool: a.tool, args: a.input,
        });
        if (out && out.action === "deny") throw new Error(out.reason || "Denied by the user from their phone.");
      }),
    );

    // 2) Permission prompts: answer from the phone while routed.
    disposers.push(
      ctx.permission.hook("evaluate", async (h) => {
        if (h.effect !== "ask") return; // opencode already decided allow/deny by its rules
        const out = await decide({
          event: "permission.ask", sessionID: h.sessionID, agent: h.agent,
          type: h.action, title: (h.resources || []).join(", "), metadata: h.metadata,
        });
        if (out && out.status) {
          h.effect = out.status;
          if (out.status === "deny" && out.reason) h.message = out.reason;
        }
      }),
    );

    // 3) Events: remember the agent's text, report idle sessions, relay replies.
    void (async () => {
      try {
        for await (const event of ctx.event.subscribe({ signal: controller.signal })) {
          const d = event.data || {};
          if (event.type === "session.text.delta" && d.sessionID) {
            const cur = lastText.get(d.sessionID);
            if (!cur || cur.messageID !== d.assistantMessageID) {
              lastText.set(d.sessionID, { messageID: d.assistantMessageID, text: d.delta || "" });
            } else {
              cur.text += d.delta || "";
            }
          } else if ((event.type === "session.idle" || event.type === "session.execution.succeeded") && d.sessionID) {
            const now = Date.now();
            if (now - (lastIdle.get(d.sessionID) || 0) < 5000) continue;
            lastIdle.set(d.sessionID, now);
            const sessionID = d.sessionID;
            let title, parentID, agent;
            try {
              const s = await ctx.session.get({ sessionID });
              title = s?.title;
              parentID = s?.parentID;
              agent = s?.agent;
            } catch {}
            if (parentID) continue; // sub-agent sessions: don't bother the phone
            // Don't block the event loop while waiting for your reply.
            (async () => {
              const out = await decide({
                event: "idle", sessionID, title, agent, lastMessage: lastText.get(sessionID)?.text || "",
              });
              if (out && out.reply) {
                try {
                  await ctx.session.prompt({ sessionID, text: out.reply });
                } catch (e) {
                  console.error("[agent-hub] prompt failed:", e?.message || e);
                }
              }
            })();
          } else if (event.type === "session.error" && d.sessionID) {
            decide({ event: "error", sessionID: d.sessionID, error: JSON.stringify(d.error || d) });
          } else if (event.type === "permission.asked" && d.sessionID) {
            decide({ event: "seen", sessionID: d.sessionID });
          }
        }
      } catch (error) {
        if (!controller.signal.aborted) console.error("[agent-hub] event stream error:", error?.message || error);
      }
    })();

    return () => {
      controller.abort();
      for (const d of disposers) {
        try {
          d?.then ? d.then((x) => x?.dispose?.()) : d?.dispose?.();
        } catch {}
      }
    };
  },
};
