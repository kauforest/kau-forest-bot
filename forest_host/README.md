# Forest auto-host helper (runs on the PC with BlueStacks 5)

`host_agent.py` drives the Forest app inside BlueStacks 5 through ADB and takes
commands from the bot over the bot's WebSocket. The bot decides when (8 min
before each study block it asks for a room, posts the link, starts at the
block time, retries once after 1 minute). See CLAUDE.md → "Forest auto-host".

Setup on the PC:
1. BlueStacks 5 (Android 11), Forest installed on the spare account, ADB on (127.0.0.1:5555).
2. `pip install websockets`
3. `python host_agent.py --selftest` (opens and closes a test room)
4. `python host_agent.py` → first run prints a secret (also saved in secret.txt, never commit it) → put it in Railway as `HOST_AGENT_SECRET`.
5. In Telegram (manager, private): `/autohost` (status), `/testroom` (private rehearsal), `/autohost on`.
