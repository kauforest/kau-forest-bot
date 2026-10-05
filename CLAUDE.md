# غابة الطب (KAU Forest Bot) — Project Context

Read this whole file before doing anything. It exists so you don't have to rediscover decisions, bugs, and gotchas that were already found and fixed over a long build session.

## What this is

A Telegram bot + companion website that gamifies competitive study-hour tracking across KAU medical school batches (Med22–Med27). Students log Forest app screenshots to the bot (which OCRs the minutes), compete on levels/streaks/batch leaderboards, and a live website shows standings. Built by Husain (an ophthalmologist, non-developer, building this as a side project — expect him to need GitHub/Railway UI walked through step by step if he's doing anything manually, but he's hands-on and tests things himself).

Launched publicly Oct 3, 2026. Group has 100+ real members. **Treat every change as touching a real, live, public system — not a prototype.**

## Architecture

- **Bot**: `forest_bot.py`, Python, `python-telegram-bot` library, deployed on Railway.
- **Website**: `docs/index.html`, single-file HTML/CSS/JS, served via **GitHub Pages from the `/docs` folder**. Styled to match the bot's navy/teal/amber theme — do not restyle without reason.
- **Database**: SQLite, persisted on a **Railway volume** mounted at `/data`, path set via `DB_PATH=/data/forest.db`. This was NOT always the case — it defaulted to ephemeral local storage early on, causing real data loss on every redeploy, until the volume was added. Never remove or change `DB_PATH` without confirming a volume is still mounted.
- **Live updates**: a WebSocket server runs **inside the same Python process** as the bot (not a separate service), listening on Railway's `$PORT`. The website connects to it directly (`wss://`) for real-time leaderboard updates. The site also falls back to a static `docs/data.json` on initial load, but that file is NOT kept fresh automatically (see `ENABLE_GITHUB_DATA_SYNC` below) — the WebSocket is the real mechanism.
- **Deployment**: Dockerfile-based, NOT Railway's auto-detected builder (Railpack). This is required because OCR needs the `tesseract-ocr` **system package**, which `pip install pytesseract` does not provide — only a Dockerfile's `apt-get install` can.

## Key environment variables (set in Railway)

```
BOT_TOKEN, GROUP_CHAT_ID, TZ_OFFSET_HOURS=3, MANAGER_IDS, LAUNCH_DATE
DB_PATH=/data/forest.db
GITHUB_TOKEN, GITHUB_REPO              # the PUBLIC repo, used for website hosting
BACKUP_GITHUB_REPO                     # a SEPARATE PRIVATE repo — see Security below
ENABLE_GITHUB_DATA_SYNC                # off by default, see note below
TOPIC_ACHIEVEMENTS_ID, TOPIC_SESSIONS_ID, TOPIC_CHAT_ID, TOPIC_SUGGESTIONS_ID
PRAYER_CITY=Jeddah, PRAYER_COUNTRY=SaudiArabia
MAX_DAILY_CARD_MINUTES=960             # 16h sanity cap on a single day's logged minutes
SUGGESTION_COOLDOWN_SECONDS=120
```

`ENABLE_GITHUB_DATA_SYNC` is intentionally off by default — the static `data.json` sync was found to be redundant with the WebSocket live-update system, AND its automated commits were triggering full Railway redeploys on every single log (since it shared `GITHUB_TOKEN`/`GITHUB_REPO` with the unrelated DB backup feature). Don't re-enable it without good reason.

## ⚠️ Security — read before touching anything GitHub-related

`kau-forest-bot` is a **PUBLIC** repo (required for free GitHub Pages). **Never push real user data, raw database files, or secrets to it.**

The automated DB backup (`backup_database_to_github()`) refuses to run at all unless `BACKUP_GITHUB_REPO` — a genuinely separate **private** repo — is explicitly configured. This was a deliberate fix after the database was briefly pushed to the public repo's `backups/` folder by mistake. If `BACKUP_GITHUB_REPO` is unset, confirm with Husain whether that cleanup (private repo creation + token scoping + variable set) was ever completed before assuming backups are safe.

## Known gotchas — do not rediscover these

1. **Railway's GitHub webhook can silently break** after renaming the GitHub account. If pushed code doesn't trigger a new deployment, check Railway → Settings → Source shows the *current* repo owner/path, and reconnect (Disconnect → re-add) if it's stale.
2. **Docker layer caching can serve stale code** even after a "successful"-looking deploy. If pushed code genuinely isn't taking effect after confirming the webhook is fine, edit the `Dockerfile` itself (bump a comment) to force a full rebuild from that layer onward.
3. **`bot_data` (python-telegram-bot's in-memory state) does NOT survive restarts.** Anything that must persist across redeploys (today's generated schedule, which message is currently pinned, streak-related state) is saved to the SQLite `settings` table via helper functions (`_save_schedule_state`, `_get_today_schedule`, etc.) — never rely on `bot_data` alone for anything that matters.
4. **Week boundaries are Sunday–Saturday** (matching the Saudi weekend Fri/Sat and the weekly leaderboard's actual Saturday-night reset) — NOT Python's default Monday-start week. Always use the `week_start_for(date)` helper. Using `date - timedelta(days=date.weekday())` directly computes the wrong boundary and was a real bug that made real, already-logged data vanish from weekly views.
5. **Arabic/RTL text bug**: embedding an LTR token (like someone's `@username`) directly inside an Arabic sentence can visually flip it (renders as `user@` instead of `@user`). Fix: prefix the token with `\u200e` (LRM) when Arabic text surrounds it on both sides, or put it on its own line. Several announcement messages had this bug; check any NEW user-facing message that mixes Arabic text with an English/username token.
6. **Telegram supergroup migration**: if the group ever gets converted to a "supergroup" (e.g. by enabling Topics), it gets a **new chat ID** — `GROUP_CHAT_ID` must be updated or every group-facing bot action silently fails with `ChatMigrated`.
7. **Daily-card OCR sanity check exists for a reason**: a real screenshot was once misread as 1,380 minutes in one day. `MAX_DAILY_CARD_MINUTES` rejects anything above a realistic daily cap — don't remove this safeguard.
8. **The display name shown publicly must always be the registered `display_name`** (their Telegram `@username` or self-chosen `/setname`), never `user.first_name` (their real Telegram name). This was a real privacy bug fixed across level-up announcements, milestones, `/stats`, and `/log` — check any NEW public-facing message doesn't reintroduce it.

## Features already built

- Screenshot-only OCR logging (Timeline sessions + daily "Focus Statistics" cards), with retroactive catch-up logging (post-launch dates only, anti-backdating via `LAUNCH_DATE`)
- Levels (Lv1→Legendary: 🥉🥈🥇💎👑), XP, streaks with milestone announcements
- Batch-vs-batch competition (raw totals + per-capita averages), weekly reset (Sunday-start), Hall of Fame
- Live WebSocket-updated public website, with daily/weekly/monthly/all-time + per-batch views
- Prayer-aware daily study schedule generator (real Jeddah prayer times via the free Aladhan API), with configurable per-weekday college-hours exclusion, weekend-specific block lengths, short-session insertion for leftover time, and 5-minute display rounding — all gap-safety-tested
- A manager-controlled kill-switch (`/enablescheduleposts` / `/pausescheduleposts`) so schedule posting can be tested privately before going live to the real group — **default is OFF/private**, don't assume it's safe to post without checking this
- `/testschedule [tomorrow|YYYY-MM-DD]`, `/checkschedule`, `/postscheduletoday` — all manager-only, all genuinely safe to run (never post publicly unless the kill-switch is on)
- Manual "تم"/"done" + automatic link-detection session completion, editing the pinned schedule message in place with strikethrough + checkmark
- Telegram Topics (forum mode) routing: achievements/sessions/chat/suggestions each have their own topic, closed to non-admins where appropriate
- A per-topic suggestion-spam cooldown (Telegram has no native per-topic slow mode — this is bot-enforced)
- `/me`, `/compare`, `/motivate`, `/mytree`, `/coffee` — light social/fun commands (NOT `/8ball` or `/fact` — explicitly removed, don't re-add without being asked)
- `/strike`, `/strikes`, `/blockmember`, `/unblockmember`, `/setforestname` — built and tested, but **explicitly deprioritized/paused** by Husain ("skip it for now, we'll handle it manually") — don't assume these are in active use

## User preferences

- Arabic-first messaging, "young adult" tone — not childish, not corporate-stiff. Husain has repeatedly asked for rewording when something reads too demanding/rhetorical (e.g. changed "من الأدمنز الحين؟" to a calmer instruction).
- Founder/early-registrant badge is **"رائد الغابة"** — not "مؤسس" (explicitly changed from that).
- Level titles are Bronze/Silver/Gold/Diamond/Legendary — explicitly NOT a copy of any reference app's naming scheme.
- Wants real testing (actual test scripts run and shown, not just "this should work") before handing off any code — this project has a long history of deployment issues that looked like code bugs but weren't, so proof matters more than confidence here.

## Where things live

- `forest_bot.py` — bot logic (repo root)
- `docs/index.html` — website (served via GitHub Pages from `/docs`)
- `Dockerfile` — required for the `tesseract-ocr` system package
- `requirements.txt` — Python deps (notably `websockets`, easy to forget when editing this)
- This file (`CLAUDE.md`) — repo root, read automatically every session

## Before starting new work, it's worth confirming with Husain

- Whether `BACKUP_GITHUB_REPO` cleanup was actually completed (private repo created, token scoped, variable set)
- Whether schedule posting (`schedule_posting_enabled`) is currently ON or OFF in the live group — don't assume
- What's actually deployed right now vs. what's been discussed/built in a chat but not yet pushed — given the deployment-lag history, always verify against GitHub directly rather than assuming the latest conversation state matches production
