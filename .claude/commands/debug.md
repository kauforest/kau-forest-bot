# /debug — Diagnose a Problem

Help Husain figure out what's wrong. Start by asking: **"What are you seeing? Copy the exact error message or describe the wrong behavior."**

Then systematically check:

## Bot issues (not responding / commands failing)
- Is the Railway service running? Check Railway dashboard → Deployments.
- Did the latest deploy succeed? Look at build logs for apt/pip errors.
- Is `BOT_TOKEN` still valid? A token reset in BotFather breaks everything silently.
- Is `GROUP_CHAT_ID` still correct? If the group was ever converted to a supergroup (Topics enabled), the chat ID changes — check Railway logs for `ChatMigrated` errors.

## Database / data issues
- Is `DB_PATH=/data/forest.db` set in Railway env vars AND is the volume actually mounted?
- Run the SQL check: read `forest_bot.py` and find the `get_db_connection()` function — confirm it's using `DB_PATH` not a hardcoded path.
- Check if user data vanished after a deploy → likely the volume isn't mounted and the bot is writing to ephemeral local storage.

## OCR / logging issues
- Is `tesseract-ocr` installed in the Docker image? Check `Dockerfile` for `apt-get install tesseract-ocr`.
- Is the screenshot being accepted? Check the `MAX_DAILY_CARD_MINUTES` cap (960 min = 16h) — extreme OCR misreads get silently rejected.
- Is the user sending a **Timeline** screenshot or a **Focus Statistics card**? Both are supported but parsed differently.

## Website issues (kauforest.github.io/kau-forest-bot)
- WebSocket disconnected? The site falls back to static `docs/data.json` — but that file may be stale if `ENABLE_GITHUB_DATA_SYNC` is off (it is off by default).
- Check browser console for WebSocket errors — the wss:// URL should point to the Railway service URL on `$PORT`.
- GitHub Pages only serves from `docs/` folder on `main` branch — confirm this in repo Settings → Pages.

## Arabic display bugs
- LTR tokens (like `@username`) inside Arabic text can visually flip — prefix with `‎` (LRM character).
- Test any new user-facing message in Telegram before considering it done.

## When to ask Husain for more info:
- If the error is in Railway logs: ask him to copy-paste the exact log lines.
- If it's a Telegram error: ask him to forward the exact bot message or screenshot.
- Never assume what's deployed — always verify against the latest `git log` on main.
