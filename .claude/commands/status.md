# /status — Check System Health

Run a quick sanity check on the whole project. Go through each of these:

## 1. Git status
```bash
git log --oneline -10
git status
```
Show Husain: what's the latest deployed commit, and are there any local uncommitted changes?

## 2. Repo structure check
Confirm these critical files are present and unmodified:
- `forest_bot.py` — main bot
- `Dockerfile` — has `apt-get install tesseract-ocr`
- `requirements.txt` — includes `websockets`
- `docs/index.html` — website
- `docs/data.json` — static fallback data
- `.claudeignore` — token savings
- `.claude/commands/` — slash commands (this folder)

## 3. Key gotchas to remind Husain about
- **Volume**: Is `DB_PATH=/data/forest.db` still set in Railway? If it ever got unset, the bot writes to ephemeral storage and all data is lost on next redeploy.
- **Backup repo**: Is `BACKUP_GITHUB_REPO` (the private repo) set in Railway? Without it, no automated DB backups are running.
- **Schedule posting**: Is `schedule_posting_enabled` ON or OFF right now? Default is OFF. Don't assume it's posting to the group.
- **Webhook**: Did the Railway ↔ GitHub webhook survive? Check Railway → Settings → Source still shows `kauforest/kau-forest-bot` (not the old username).

## 4. Known deprioritized features (don't re-enable without being asked)
- `/strike`, `/strikes`, `/blockmember`, `/unblockmember` — built but paused
- `ENABLE_GITHUB_DATA_SYNC` — off intentionally (was causing Railway redeploys on every log)
- `/8ball`, `/fact` — explicitly removed, don't re-add

## 5. What to check if something feels off
- Real members: 100+ in the Telegram group
- Public site: kauforest.github.io/kau-forest-bot
- Week boundary: Sunday–Saturday (Saudi calendar), using `week_start_for(date)` helper
- Level system: Bronze → Silver → Gold → Diamond → Legendary (NOT copied from any other app)
