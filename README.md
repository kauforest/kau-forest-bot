# غابة الطب — KAU Forest Bot

A Telegram bot (**@KAUForestBot**) and a live website that turn study time into a friendly competition across KAU medical batches (Med22–Med27). Members send Forest app screenshots; the bot reads the minutes (OCR) and updates levels, streaks, batch standings and the website.

> This repo is **public** (needed for free GitHub Pages). Never commit database files, member data or secrets. Database backups go to a separate **private** repo (`BACKUP_GITHUB_REPO`).

For full technical context (architecture, gotchas, bug history) read [`CLAUDE.md`](CLAUDE.md). For a quick Arabic recovery guide see [`ACCESS_AND_RECOVERY.md`](ACCESS_AND_RECOVERY.md).

## What's where

| Part | Where |
|---|---|
| Bot code | `forest_bot.py` (python-telegram-bot 21.6, job-queue) |
| Website | `docs/index.html`, served by GitHub Pages from `/docs` |
| Live website updates | WebSocket server inside the bot process, on Railway's `$PORT` |
| Hosting | Railway, built from the `Dockerfile` (installs `tesseract-ocr` for OCR and `libfribidi0` for Arabic text on `/mycard`) |
| Database | SQLite on a Railway volume: `DB_PATH=/data/forest.db`. Don't change this without confirming the volume. |
| Card fonts | `assets/fonts/` (Tajawal) |
| Offline tests | `python3 tests/test_bot_offline.py` (fake Telegram, temp DB, nothing is sent) |

## Deploying a change

1. Merge the pull request on GitHub (or commit to `main`).
2. Railway rebuilds and redeploys automatically.
3. If new code doesn't seem live: check Railway → Settings → Source points at `kauforest/kau-forest-bot`, then bump the cache-bust comment in the `Dockerfile`.

## Environment variables (Railway → Variables)

| Variable | Notes |
|---|---|
| `BOT_TOKEN` | From BotFather |
| `GROUP_CHAT_ID` | Changes if the group is ever converted to a supergroup |
| `TZ_OFFSET_HOURS` | `3` |
| `MANAGER_IDS` | Comma-separated Telegram user IDs |
| `LAUNCH_DATE` | Screenshots dated before this are rejected |
| `DB_PATH` | `/data/forest.db` |
| `GITHUB_TOKEN`, `GITHUB_REPO` | Public repo (website) |
| `BACKUP_GITHUB_REPO` | Separate **private** repo. Backups refuse to run without it. |
| `ENABLE_GITHUB_DATA_SYNC` | Leave off — its commits caused a redeploy on every log |
| `TOPIC_ACHIEVEMENTS_ID`, `TOPIC_SESSIONS_ID`, `TOPIC_CHAT_ID`, `TOPIC_SUGGESTIONS_ID`, `TOPIC_ANNOUNCEMENTS_ID` | Forum topic IDs |
| `PRAYER_CITY`, `PRAYER_COUNTRY` | `Jeddah`, `SaudiArabia` (schedule generator) |
| `MAX_DAILY_CARD_MINUTES` | `960` — sanity cap after a real 1,380-minute OCR misread |
| `SUGGESTION_COOLDOWN_SECONDS` | `120` |

## Logging (screenshot-only)

- **Timeline entry** screenshot → adds that session's minutes.
- **Daily "Focus Statistics" card** → replaces that day's total (the card wins, nothing double-counts).
- Anti-cheat: image hash, same-session time-window check, date check, `LAUNCH_DATE` gate, daily cap. Confirm/edit buttons before anything is saved.
- First-time senders pick their batch with buttons.

## Commands

**Members:** `/register Med25`, `/setname`, `/stats`, `/me`, `/mycard`, `/leaderboard [Med25]`, `/findpartner <subject>`, `/compare`, `/motivate`, `/mytree`, `/coffee`, `/nextsession`

**Managers:** `/log` (reply, manual fix), `/exammode on|off`, `/setcollegehours`, `/setschedule`, `/testschedule [tomorrow|YYYY-MM-DD]` (private only), `/checkschedule`, `/postscheduletoday`, `/enablescheduleposts`, `/pausescheduleposts` (kill-switch, default OFF), `/testpoll`

**Moderation (built, currently paused):** `/strike`, `/strikes`, `/setforestname`, `/blockmember`, `/unblockmember`

## Automatic posts (Jeddah time)

| When | What |
|---|---|
| Daily 00:05 | Prayer-aware study schedule (only if posting is enabled) |
| Daily 00:15 | Private nudge to anyone whose 7+ day streak broke |
| Daily 04:00 | Database backup to the private repo |
| Daily 17:00 | "Which tree?" poll |
| Daily 21:00 | Private streak-save reminder if a 7+ day streak isn't logged yet today |
| Daily 23:00 | Daily summary |
| Saturday 23:30 | Weekly leaderboard, Hall of Fame, batch vs batch |
| Sunday 10:00 | Personal weekly recap DM |
| 1st of month 09:00 / 09:15 | Monthly recap, then monthly titles (نجم الشهر / رواد الشهر) |
| Every minute | Ping 5 minutes before a schedule block |

## Levels and badges

Bronze → Silver → Gold → Diamond → Legendary (🥉🥈🥇💎👑), XP, streaks and milestones. The first 15 members of each batch get the «رائد الغابة» badge.
