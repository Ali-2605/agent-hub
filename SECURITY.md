# Security

agent-hub lets a phone control coding agents and scripts on your computer, so it is built to fail closed.

## Threat model

| Threat | What stops it |
|---|---|
| Someone on the internet finds the hub | No open ports. The REST API listens on 127.0.0.1 only, and Telegram is reached by outgoing long polling. |
| A local program or web page calls the API | Every route except `/api/health` needs the random token in `~/.config/agent-hub/token` (mode 600), checked in constant time. |
| A stranger messages your bot | Only your private chat is obeyed: the chat id *and* the sender id must match, and group chats are ignored. |
| **Someone gets into your Telegram account** (stolen session, SIM swap, unlocked phone) | **Phone lock.** After every restart only read-only commands and `/off` work. Acting needs `/unlock <PIN>`. The PIN is set only on the laptop, hashed (PBKDF2, 600k iterations), deleted from the chat after use, and never logged. Unlocks expire, and 5 wrong PINs turn the hub off. `bypassPermissions` can't be enabled from the phone unless you opt in. |
| Your bot token leaks | A second poller triggers Telegram's `409 Conflict`: the hub locks the phone and alerts you to revoke the token. The token is redacted from logs and errors. A leaked token can't forge messages from you, but whoever holds it can read what you send the bot (including a PIN) while they poll, so revoke it immediately. |
| An agent or the phone exfiltrates secrets | `send_file` and `/get` refuse credential folders, private keys, `.env*`, `.netrc`, `.git-credentials`, `*.tfstate`, and the hub's own folder. `/get` only reaches registered projects and agent working folders. |
| Shell injection through task arguments | Tasks run as argv without a shell. There is no "run any command" feature. |
| The hub starts without you knowing | It only starts with the start password, typed on the laptop (`ahub on`). The phone can turn it off, never on. |

**Out of scope:** someone who already controls your user account on the laptop. They can read the hub's files and edit its code. Lock your computer.

## Recommended setup

1. `ahub password` and `ahub pin`, using two different secrets.
2. Turn on **Two-Step Verification** in Telegram (Settings → Privacy and Security).
3. Keep `phone_allow_bypass: false` and `phone_claude_mode: default`.
4. Lock your phone. Use `/lock` when you're done, or rely on the 15-minute expiry.
5. If you ever suspect the token leaked: @BotFather → `/mybots` → your bot → API Token → **Revoke**, then run `ahub setup-telegram` again.

## Reporting a vulnerability

Please email **ali.ehabezzeldin@gmail.com** instead of opening a public issue. I'll reply as soon as I can.
