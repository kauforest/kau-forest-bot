"""
Forest Study Group Bot — Cross-Batch Edition
---------------------------------------------
Tracks focus sessions across medical batches (Med22-Med27). Logging is
screenshot-only by design: forwarding a Forest completion screenshot gets
OCR'd for minutes + a lightweight date check, and each image's hash is
recorded so the same screenshot can never be logged twice. First-time
senders are auto-registered (batch chosen via button, no /register
needed). Managers can still `/log` on someone's behalf as a manual
correction by replying to their message.

Posts daily/weekly/monthly stats, per-batch leaderboards (both raw totals
and a fairer per-capita average), a permanent Hall of Fame, founder
badges, milestone shoutouts, and a daily "which tree did you plant" poll.
Two nominated "managers" post the day's flexible study window with
/setschedule, and can toggle a gentler /exammode during exam weeks.

Setup:
    1. pip install -r requirements.txt
    2. Set the BOT_TOKEN and GROUP_CHAT_ID environment variables
       (see README.md for how to get these)
    3. Run: python forest_bot.py

Requires tesseract-ocr to be installed on the host system for screenshot
reading (see README.md) — without it, logging is unavailable, since
screenshots are now the only way to log a session.
"""

import os
import re
import sqlite3
import base64
import json
import logging
from datetime import datetime, date, timedelta, time as dtime
from io import BytesIO

try:
    import requests

    REQUESTS_AVAILABLE = True
except ImportError:
    REQUESTS_AVAILABLE = False

try:
    import websockets

    WEBSOCKETS_AVAILABLE = True
except ImportError:
    WEBSOCKETS_AVAILABLE = False

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

# Optional OCR support
try:
    from PIL import Image
    import pytesseract

    OCR_AVAILABLE = True
except ImportError:
    OCR_AVAILABLE = False

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
GROUP_CHAT_ID = os.environ.get("GROUP_CHAT_ID", "")  # e.g. -1001234567890

# Telegram Topics (forum-mode threads) — optional. Leave unset and
# everything posts to the group's default "General" topic as normal.
# Once Topics are enabled on the group, get each topic's numeric ID by
# opening it -> copy link -> the number at the end of the link.
TOPIC_ACHIEVEMENTS_ID = os.environ.get("TOPIC_ACHIEVEMENTS_ID", "")
TOPIC_SESSIONS_ID = os.environ.get("TOPIC_SESSIONS_ID", "")
TOPIC_CHAT_ID = os.environ.get("TOPIC_CHAT_ID", "")  # 💬 الدردشة العامة — used by the daily poll
TOPIC_SUGGESTIONS_ID = os.environ.get("TOPIC_SUGGESTIONS_ID", "")  # 💡 أفكار واقتراحات — has its own cooldown
SUGGESTION_COOLDOWN_SECONDS = int(os.environ.get("SUGGESTION_COOLDOWN_SECONDS", "120"))
TOPIC_ANNOUNCEMENTS_ID = os.environ.get("TOPIC_ANNOUNCEMENTS_ID", "")


def _topic_kwargs(topic_id: str) -> dict:
    """Returns {} when a topic isn't configured (message posts to General,
    nothing breaks), or {'message_thread_id': ...} when it is."""
    return {"message_thread_id": int(topic_id)} if topic_id else {}
DB_PATH = os.environ.get("DB_PATH", "forest.db")

# Telegram user IDs of the people allowed to set the daily study schedule.
# Comma-separated, e.g. "111111111,222222222". Get a user's ID by having
# them message @userinfobot.
MANAGER_IDS = {
    int(x) for x in os.environ.get("MANAGER_IDS", "").split(",") if x.strip().isdigit()
}

VALID_BATCHES = ["Med22", "Med23", "Med24", "Med25", "Med26", "Med27"]

# Optional: push a data.json snapshot to a GitHub repo so a static
# GitHub Pages site can display the leaderboard publicly. Leave any of
# these unset to disable website syncing entirely (the bot still works).
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
GITHUB_REPO = os.environ.get("GITHUB_REPO", "")  # e.g. "yourname/study-leaderboard" — this is PUBLIC, used for the website
BACKUP_GITHUB_REPO = os.environ.get("BACKUP_GITHUB_REPO", "")  # MUST be a separate PRIVATE repo — real user data goes here
GITHUB_BRANCH = os.environ.get("GITHUB_BRANCH", "main")
GITHUB_DATA_PATH = os.environ.get("GITHUB_DATA_PATH", "data.json")
# Off by default — the WebSocket live-update system already handles the
# site staying fresh, so this static-JSON sync is redundant and its
# commits were triggering a full Railway redeploy on every single log
# (since it shares GITHUB_TOKEN/GITHUB_REPO with the unrelated DB backup
# feature). Set to "true" only if you specifically want the static
# fallback file kept fresh too, independent of DB backups.
ENABLE_GITHUB_DATA_SYNC = os.environ.get("ENABLE_GITHUB_DATA_SYNC", "").lower() == "true"

# Official launch date (YYYY-MM-DD). Before this date, logging is blocked
# entirely — registration still works (early joiners still get founder
# badges), but no minutes can be claimed. This exists specifically so no
# one can screenshot old Forest history from before the competition
# existed and backdate it in. Leave unset to disable the gate (logging
# always allowed) — useful while testing before you've picked a date.
LAUNCH_DATE = os.environ.get("LAUNCH_DATE", "")  # e.g. "2026-10-05"


def is_before_launch() -> bool:
    if not LAUNCH_DATE:
        return False
    try:
        return local_today() < date.fromisoformat(LAUNCH_DATE)
    except ValueError:
        return False

# Timezone offset from UTC in hours, for scheduling posts at local time.
# Saudi Arabia is UTC+3. Change this if your group is elsewhere.
TZ_OFFSET_HOURS = int(os.environ.get("TZ_OFFSET_HOURS", "3"))

MILESTONES_MINUTES = [600, 1500, 3000, 6000]  # 10h, 25h, 50h, 100h
STREAK_MILESTONES = [3, 7, 14, 30, 60]
STREAK_LABELS = {
    3: "🔥 3 أيام متتالية — بداية موفقة!",
    7: "🔥🔥 أسبوع كامل بدون انقطاع!",
    14: "🔥🔥 أسبوعين متتاليين — التزام حقيقي!",
    30: "🔥🔥🔥 شهر كامل من الاستمرارية — أسطوري!",
    60: "🔥🔥🔥 شهرين متتاليين — ما في وصف لهذا!",
}
MILESTONE_LABELS = {
    600: "🌱 10 ساعات تركيز — أول علامة فارقة!",
    1500: "🌿 25 ساعة تركيز — استمرار رائع!",
    3000: "🌳 50 ساعة تركيز — إنجاز كبير!",
    6000: "🌲 100 ساعة تركيز — أسطورة الغابة!",
}

# Level system — grows with the square root of all-time hours (fast early
# wins, naturally slower later, same shape as most XP curves). Titles are
# our own forest-growth naming, deliberately NOT a "Med Student / Clerk /
# Resident" copy of any other app's leaderboard wording — this one's ours.
LEVEL_TITLES = [
    (1, "🥉 برونزي"),
    (4, "🥈 فضي"),
    (8, "🥇 ذهبي"),
    (13, "💎 ماسي"),
    (19, "👑 أسطوري"),
]

# Bonus XP per day of an ongoing streak — rewards consistency, not just
# raw volume in one sitting (same idea as "streak bonuses included" on
# any XP-leaderboard app).
STREAK_XP_PER_DAY = 3


def level_for_total(total_minutes: int) -> int:
    hours = total_minutes / 60
    return 1 + int(hours ** 0.5)


def level_title_for(level: int) -> str:
    title = LEVEL_TITLES[0][1]
    for threshold, name in LEVEL_TITLES:
        if level >= threshold:
            title = name
    return title


def minutes_for_next_level(total_minutes: int) -> int:
    """Minutes still needed to reach the next level, for a 'so close!' nudge."""
    current_level = level_for_total(total_minutes)
    next_hours_needed = current_level ** 2  # inverse of the sqrt curve
    next_minutes_needed = next_hours_needed * 60
    return max(0, next_minutes_needed - total_minutes)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------


def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = db()
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            username TEXT,
            minutes INTEGER NOT NULL,
            tag TEXT,
            logged_at TEXT NOT NULL,   -- ISO datetime (UTC)
            session_date TEXT NOT NULL, -- local date, YYYY-MM-DD
            source TEXT NOT NULL DEFAULT 'session' -- 'session' | 'daily_card' | 'manual'
        )
        """
    )
    # Lightweight migration for DBs created before the `source` column existed.
    try:
        conn.execute("ALTER TABLE sessions ADD COLUMN source TEXT NOT NULL DEFAULT 'session'")
    except sqlite3.OperationalError:
        pass
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS milestones_hit (
            user_id INTEGER NOT NULL,
            milestone INTEGER NOT NULL,
            PRIMARY KEY (user_id, milestone)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS streak_milestones_hit (
            user_id INTEGER NOT NULL,
            milestone INTEGER NOT NULL,
            PRIMARY KEY (user_id, milestone)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS levels_hit (
            user_id INTEGER NOT NULL,
            level INTEGER NOT NULL,
            PRIMARY KEY (user_id, level)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            display_name TEXT NOT NULL,
            batch TEXT NOT NULL,
            founder INTEGER DEFAULT 0,
            registered_at TEXT NOT NULL
        )
        """
    )
    try:
        conn.execute("ALTER TABLE users ADD COLUMN forest_username TEXT")
    except sqlite3.OperationalError:
        pass  # already added by a previous startup — ALTER TABLE has no IF NOT EXISTS in SQLite
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS hall_of_fame (
            week_start TEXT PRIMARY KEY,
            user_id INTEGER,
            name TEXT,
            batch TEXT,
            minutes INTEGER
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS screenshots (
            hash TEXT PRIMARY KEY,
            user_id INTEGER,
            used_at TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS college_hours (
            weekday INTEGER PRIMARY KEY,
            start_time TEXT,
            end_time TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS strikes (
            user_id INTEGER PRIMARY KEY,
            count INTEGER DEFAULT 0,
            last_strike_at TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS suggestion_cooldowns (
            user_id INTEGER PRIMARY KEY,
            last_posted_at TEXT
        )
        """
    )
    conn.commit()
    conn.close()


WEEKDAY_NAMES = {
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
    "friday": 4, "saturday": 5, "sunday": 6,
}
WEEKDAY_NAMES_AR = ["الاثنين", "الثلاثاء", "الأربعاء", "الخميس", "الجمعة", "السبت", "الأحد"]


def get_college_hours(weekday: int):
    """Recurring weekly college-hours window for this weekday, or None if
    not set. Weekly (not daily) because university timetables repeat by
    weekday, not change every single day — set once, applies every week."""
    conn = db()
    row = conn.execute(
        "SELECT start_time, end_time FROM college_hours WHERE weekday=?", (weekday,)
    ).fetchone()
    conn.close()
    return (row["start_time"], row["end_time"]) if row else None


def set_college_hours(weekday: int, start_time: str | None, end_time: str | None):
    conn = db()
    if start_time is None:
        conn.execute("DELETE FROM college_hours WHERE weekday=?", (weekday,))
    else:
        conn.execute(
            "INSERT INTO college_hours (weekday, start_time, end_time) VALUES (?, ?, ?) "
            "ON CONFLICT(weekday) DO UPDATE SET start_time=excluded.start_time, end_time=excluded.end_time",
            (weekday, start_time, end_time),
        )
    conn.commit()
    conn.close()


def is_screenshot_used(hash_hex: str) -> bool:
    conn = db()
    row = conn.execute("SELECT 1 FROM screenshots WHERE hash=?", (hash_hex,)).fetchone()
    conn.close()
    return row is not None


def mark_screenshot_used(hash_hex: str, user_id: int):
    conn = db()
    conn.execute(
        "INSERT OR IGNORE INTO screenshots (hash, user_id, used_at) VALUES (?, ?, ?)",
        (hash_hex, user_id, datetime.utcnow().isoformat()),
    )
    conn.commit()
    conn.close()


FOUNDER_SLOTS_PER_BATCH = 15


def batch_member_count(batch: str) -> int:
    conn = db()
    row = conn.execute("SELECT COUNT(*) AS c FROM users WHERE batch=?", (batch,)).fetchone()
    conn.close()
    return row["c"]


def get_setting(key: str, default=None):
    conn = db()
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    conn.close()
    return row["value"] if row else default


def set_setting(key: str, value: str):
    conn = db()
    conn.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )
    conn.commit()
    conn.close()


def record_hall_of_fame(week_start: date, user_id: int, name: str, batch: str, minutes: int):
    conn = db()
    conn.execute(
        "INSERT INTO hall_of_fame (week_start, user_id, name, batch, minutes) VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT(week_start) DO UPDATE SET user_id=excluded.user_id, name=excluded.name, "
        "batch=excluded.batch, minutes=excluded.minutes",
        (week_start.isoformat(), user_id, name, batch, minutes),
    )
    conn.commit()
    conn.close()


def get_hall_of_fame(limit: int = 12):
    conn = db()
    rows = conn.execute(
        "SELECT * FROM hall_of_fame ORDER BY week_start DESC LIMIT ?", (limit,)
    ).fetchall()
    conn.close()
    return rows


def get_user(user_id: int):
    conn = db()
    row = conn.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()
    conn.close()
    return row


def register_user(user_id: int, display_name: str, batch: str) -> bool:
    """Returns True if this registration earned a founder badge (first
    FOUNDER_SLOTS_PER_BATCH people to register in that batch)."""
    is_founder = batch_member_count(batch) < FOUNDER_SLOTS_PER_BATCH
    conn = db()
    conn.execute(
        "INSERT INTO users (user_id, display_name, batch, founder, registered_at) "
        "VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT(user_id) DO UPDATE SET batch=excluded.batch",
        (user_id, display_name, batch, int(is_founder), datetime.utcnow().isoformat()),
    )
    conn.commit()
    conn.close()
    return is_founder


def set_display_name(user_id: int, display_name: str):
    conn = db()
    conn.execute("UPDATE users SET display_name=? WHERE user_id=?", (display_name, user_id))
    conn.commit()
    conn.close()


def local_today() -> date:
    return (datetime.utcnow() + timedelta(hours=TZ_OFFSET_HOURS)).date()


def local_now() -> datetime:
    return datetime.utcnow() + timedelta(hours=TZ_OFFSET_HOURS)


def week_start_for(d: date) -> date:
    """Most recent Sunday on/before d — matches the Saudi week (weekend
    Fri/Sat, work week Sun-Thu) and the weekly leaderboard's actual
    Saturday-night reset. Python's own weekday() is Monday-start (ISO),
    which does NOT match this — using it directly silently resets 'this
    week' a day early (Monday) instead of Sunday, which is exactly what
    made real, already-logged data vanish from the weekly view."""
    return d - timedelta(days=(d.weekday() + 1) % 7)


def log_session(user_id: int, username: str, minutes: int, tag: str | None):
    """A single Forest-session entry (Timeline screenshot). Multiple of
    these on the same day ADD UP — unless a daily_card entry exists for
    that day, in which case the card's total wins (see _effective_cte)."""
    conn = db()
    conn.execute(
        "INSERT INTO sessions (user_id, username, minutes, tag, logged_at, session_date, source) "
        "VALUES (?, ?, ?, ?, ?, ?, 'session')",
        (
            user_id,
            username,
            minutes,
            tag,
            datetime.utcnow().isoformat(),
            local_today().isoformat(),
        ),
    )
    conn.commit()
    conn.close()


def log_daily_card(user_id: int, username: str, minutes: int, session_date: date | None = None):
    """A Forest 'Focus Statistics' daily-total screenshot. Replaces (never
    adds to) any earlier daily_card for the SAME user+day, since it's
    already a cumulative total for that day, not one more session.

    session_date defaults to today, but can be any past date — this is
    what makes catch-up logging possible: someone can grind all day, then
    send yesterday's (or any earlier day's) card at the end. Re-sending
    the same day's card later just revises that day; it never adds twice."""
    day_str = (session_date or local_today()).isoformat()
    conn = db()
    conn.execute(
        "DELETE FROM sessions WHERE user_id=? AND session_date=? AND source='daily_card'",
        (user_id, day_str),
    )
    conn.execute(
        "INSERT INTO sessions (user_id, username, minutes, tag, logged_at, session_date, source) "
        "VALUES (?, ?, ?, NULL, ?, ?, 'daily_card')",
        (user_id, username, minutes, datetime.utcnow().isoformat(), day_str),
    )
    conn.commit()
    conn.close()


# Reconciliation: for any user+day where a daily_card screenshot exists,
# that number is the truth for the day and per-session rows for that same
# day are ignored (they'd otherwise double-count what the card already
# totals). Days with no daily_card just sum their session rows as before.
_EFFECTIVE_CTE = """
    WITH daily_effective AS (
        SELECT user_id, session_date,
            COALESCE(
                MAX(CASE WHEN source='daily_card' THEN minutes END),
                SUM(CASE WHEN source!='daily_card' THEN minutes ELSE 0 END)
            ) AS minutes
        FROM sessions
        WHERE session_date >= :since
        GROUP BY user_id, session_date
    )
"""


def daily_top_achievers(for_date: date, limit: int = 3):
    """Top performers for ONE specific day (not a running total since a
    date, like leaderboard() does) — for the 'top achiever(s) of the day'
    callout in the daily summary."""
    conn = db()
    query = (
        _EFFECTIVE_CTE
        + """
        SELECT de.user_id,
               COALESCE(u.display_name, de.user_id) AS name,
               COALESCE(u.batch, '') AS batch,
               de.minutes AS total
        FROM daily_effective de
        LEFT JOIN users u ON u.user_id = de.user_id
        WHERE de.session_date = :since
        ORDER BY de.minutes DESC
        LIMIT :limit
        """
    )
    rows = conn.execute(query, {"since": for_date.isoformat(), "limit": limit}).fetchall()
    conn.close()
    return rows


def total_minutes(user_id: int) -> int:
    conn = db()
    row = conn.execute(
        _EFFECTIVE_CTE
        + "SELECT COALESCE(SUM(minutes),0) AS m FROM daily_effective WHERE user_id=:uid",
        {"since": "2000-01-01", "uid": user_id},
    ).fetchone()
    conn.close()
    return row["m"]


def leaderboard(since_date: date, limit: int = 10, batch: str | None = None):
    conn = db()
    query = (
        _EFFECTIVE_CTE
        + """
        SELECT de.user_id,
               COALESCE(u.display_name, de.user_id) AS name,
               COALESCE(u.batch, '') AS batch,
               SUM(de.minutes) AS total
        FROM daily_effective de
        LEFT JOIN users u ON u.user_id = de.user_id
        """
    )
    params = {"since": since_date.isoformat()}
    if batch:
        query += " WHERE u.batch = :batch"
        params["batch"] = batch
    query += " GROUP BY de.user_id ORDER BY total DESC LIMIT :limit"
    params["limit"] = limit
    rows = conn.execute(query, params).fetchall()
    conn.close()
    return rows


def batches_with_activity(since_date: date):
    conn = db()
    rows = conn.execute(
        """
        SELECT DISTINCT u.batch FROM sessions s
        JOIN users u ON u.user_id = s.user_id
        WHERE s.session_date >= ? AND u.batch IS NOT NULL
        """,
        (since_date.isoformat(),),
    ).fetchall()
    conn.close()
    return [r["batch"] for r in rows]


def most_improved(this_week_start: date, last_week_start: date):
    """Return the user with the largest increase vs the prior week."""
    conn = db()
    this_week = {
        r["user_id"]: r["total"]
        for r in conn.execute(
            _EFFECTIVE_CTE
            + "SELECT user_id, SUM(minutes) AS total FROM daily_effective GROUP BY user_id",
            {"since": this_week_start.isoformat()},
        ).fetchall()
    }
    last_week_query = """
        WITH daily_effective AS (
            SELECT user_id, session_date,
                COALESCE(
                    MAX(CASE WHEN source='daily_card' THEN minutes END),
                    SUM(CASE WHEN source!='daily_card' THEN minutes ELSE 0 END)
                ) AS minutes
            FROM sessions
            WHERE session_date >= :since AND session_date < :until
            GROUP BY user_id, session_date
        )
        SELECT user_id, SUM(minutes) AS total FROM daily_effective GROUP BY user_id
    """
    last_week = {
        r["user_id"]: r["total"]
        for r in conn.execute(
            last_week_query,
            {"since": last_week_start.isoformat(), "until": this_week_start.isoformat()},
        ).fetchall()
    }
    names = {
        r["user_id"]: r["name"]
        for r in conn.execute(
            "SELECT s.user_id AS user_id, COALESCE(u.display_name, s.username) AS name "
            "FROM sessions s LEFT JOIN users u ON u.user_id = s.user_id "
            "WHERE s.session_date >= ? GROUP BY s.user_id",
            (last_week_start.isoformat(),),
        ).fetchall()
    }
    conn.close()
    best_user, best_delta = None, 0
    for uid, total in this_week.items():
        delta = total - last_week.get(uid, 0)
        if delta > best_delta:
            best_delta, best_user = delta, names.get(uid, str(uid))
    return best_user, best_delta


def current_streak(user_id: int, as_of: date | None = None) -> int:
    """Consecutive logged days ending at as_of (defaults to today).
    Passing a past date computes what the streak was/would have been as
    of that day — used by the streak-break detector below, which needs
    to look at 'as of yesterday' and 'as of the day before' separately."""
    conn = db()
    rows = conn.execute(
        "SELECT DISTINCT session_date FROM sessions WHERE user_id=? ORDER BY session_date DESC",
        (user_id,),
    ).fetchall()
    conn.close()
    dates = {date.fromisoformat(r["session_date"]) for r in rows}
    streak = 0
    cursor = as_of or local_today()
    while cursor in dates:
        streak += 1
        cursor -= timedelta(days=1)
    return streak


def get_top_streaks(limit: int = 8):
    """Top current streaks across all registered users. O(n) over the
    user list — fine at this scale, revisit if it ever gets huge."""
    conn = db()
    users = conn.execute("SELECT user_id, display_name, batch FROM users").fetchall()
    conn.close()
    results = []
    for u in users:
        s = current_streak(u["user_id"])
        if s > 0:
            results.append({"name": u["display_name"], "batch": u["batch"], "streak": s})
    results.sort(key=lambda r: r["streak"], reverse=True)
    return results[:limit]


async def check_and_announce_streak_milestones(update, context, user):
    streak = current_streak(user.id)
    conn = db()
    for m in STREAK_MILESTONES:
        if streak >= m:
            already = conn.execute(
                "SELECT 1 FROM streak_milestones_hit WHERE user_id=? AND milestone=?",
                (user.id, m),
            ).fetchone()
            if not already:
                conn.execute(
                    "INSERT INTO streak_milestones_hit (user_id, milestone) VALUES (?, ?)",
                    (user.id, m),
                )
                conn.commit()
                if GROUP_CHAT_ID:
                    registered = get_user(user.id)
                    public_name = registered["display_name"] if registered else user.first_name
                    await context.bot.send_message(
                        chat_id=GROUP_CHAT_ID,
                        text=f"{STREAK_LABELS[m]}\n\u200e{public_name} مستمر بدون انقطاع!",
                        **_topic_kwargs(TOPIC_ACHIEVEMENTS_ID),
                    )
    conn.close()


async def check_and_announce_level_up(update, context, user):
    total = total_minutes(user.id)
    level = level_for_total(total)
    if level <= 1:
        return
    conn = db()
    already = conn.execute(
        "SELECT 1 FROM levels_hit WHERE user_id=? AND level=?", (user.id, level)
    ).fetchone()
    if not already:
        conn.execute("INSERT INTO levels_hit (user_id, level) VALUES (?, ?)", (user.id, level))
        conn.commit()
        title = level_title_for(level)
        if GROUP_CHAT_ID:
            registered = get_user(user.id)
            public_name = registered["display_name"] if registered else user.first_name
            await context.bot.send_message(
                chat_id=GROUP_CHAT_ID,
                text=f"⭐ ترقية! \u200e{public_name} صار بالمستوى {level} — {title}",
                **_topic_kwargs(TOPIC_ACHIEVEMENTS_ID),
            )
    conn.close()


def _rows_to_list(rows):
    out = []
    for r in rows:
        conn = db()
        founder_row = conn.execute(
            "SELECT founder FROM users WHERE user_id=?", (r["user_id"],)
        ).fetchone()
        conn.close()
        all_time = total_minutes(r["user_id"])  # level reflects career-to-date, not just this period
        level = level_for_total(all_time)
        streak_bonus = current_streak(r["user_id"]) * STREAK_XP_PER_DAY
        out.append({
            "name": r["name"],
            "batch": r["batch"] or None,
            "minutes": r["total"],
            "founder": bool(founder_row and founder_row["founder"]),
            "level": level,
            "level_title": level_title_for(level),
            "xp": r["total"] + streak_bonus,
        })
    return out


def batch_totals(since_date: date) -> dict:
    """Raw sum per batch — deliberately kept alongside the per-capita average:
    a batch can climb this one just by recruiting more people, which is the
    point (it's the growth/recruitment incentive)."""
    conn = db()
    rows = conn.execute(
        _EFFECTIVE_CTE
        + """
        SELECT u.batch, SUM(de.minutes) AS total
        FROM daily_effective de JOIN users u ON u.user_id = de.user_id
        GROUP BY u.batch
        """,
        {"since": since_date.isoformat()},
    ).fetchall()
    conn.close()
    return {r["batch"]: r["total"] for r in rows}


def batch_averages(since_date: date):
    """Average minutes per registered member, per batch — fairer than a raw
    sum since batches don't all have the same number of people signed up."""
    totals = batch_totals(since_date)
    result = {}
    for batch, total in totals.items():
        count = batch_member_count(batch)
        if count:
            result[batch] = round(total / count, 1)
    return result


def build_export_data() -> dict:
    today = local_today()
    week_start = week_start_for(today)
    month_start = today.replace(day=1)
    epoch = date(2000, 1, 1)

    weekly_by_batch = {}
    for batch in VALID_BATCHES:
        rows = leaderboard(week_start, limit=50, batch=batch)
        if rows:
            weekly_by_batch[batch] = _rows_to_list(rows)

    last_week_start = week_start - timedelta(days=7)
    improved_name, improved_delta = most_improved(week_start, last_week_start)

    hof_rows = get_hall_of_fame(limit=12)
    hall_of_fame = [
        {"week_start": r["week_start"], "name": r["name"], "batch": r["batch"], "minutes": r["minutes"]}
        for r in hof_rows
    ]

    return {
        "generated_at": datetime.utcnow().isoformat(),
        "exam_mode": get_setting("exam_mode", "off") == "on",
        "daily": _rows_to_list(leaderboard(today, limit=50)),
        "weekly": _rows_to_list(leaderboard(week_start, limit=50)),
        "monthly": _rows_to_list(leaderboard(month_start, limit=50)),
        "all_time": _rows_to_list(leaderboard(epoch, limit=50)),
        "weekly_by_batch": weekly_by_batch,
        "all_batches": VALID_BATCHES,  # always the full list — so a quiet batch's filter chip never disappears
        "weekly_batch_averages": batch_averages(week_start),
        "weekly_batch_totals": batch_totals(week_start),
        "hall_of_fame": hall_of_fame,
        "most_improved": {"name": improved_name, "delta": improved_delta} if improved_name else None,
        "top_streaks": get_top_streaks(limit=8),
    }


CONNECTED_CLIENTS: set = set()


async def broadcast_update():
    """Pushes the current leaderboard to every browser tab with the site
    open, over the WebSocket server started in main(). This is what makes
    the site update live instead of only on page refresh. Safe to call
    even if no server is running / no clients are connected."""
    if not CONNECTED_CLIENTS:
        return
    try:
        payload = json.dumps(build_export_data(), ensure_ascii=False)
    except Exception:
        logger.exception("Failed to build export data for broadcast")
        return
    dead = set()
    for ws in CONNECTED_CLIENTS:
        try:
            await ws.send(payload)
        except Exception:
            dead.add(ws)
    CONNECTED_CLIENTS.difference_update(dead)


async def ws_handler(websocket):
    """One entry per connected browser tab. Sends the current leaderboard
    immediately on connect, then just keeps the socket open — all further
    updates come from broadcast_update() being called elsewhere whenever
    someone logs a session."""
    CONNECTED_CLIENTS.add(websocket)
    try:
        payload = json.dumps(build_export_data(), ensure_ascii=False)
        await websocket.send(payload)
        async for _ in websocket:
            pass  # the site never sends us anything; just keep the connection open
    except Exception:
        pass
    finally:
        CONNECTED_CLIENTS.discard(websocket)


async def start_ws_server(app):
    """Runs the live-update WebSocket server on Railway's assigned $PORT,
    alongside the bot's own polling loop, for as long as the app runs."""
    if not WEBSOCKETS_AVAILABLE:
        logger.warning("websockets package not installed — live updates disabled")
        return
    port = int(os.environ.get("PORT", 8765))
    server = await websockets.serve(ws_handler, "0.0.0.0", port)
    app.bot_data["ws_server"] = server
    logger.info(f"Live-update WebSocket server listening on 0.0.0.0:{port}")


def _push_file_to_github(repo_path: str, content_bytes: bytes, commit_message: str, repo: str | None = None) -> bool:
    """Shared GitHub Contents API push (used by both the leaderboard sync
    and the DB backup below). Silently no-ops if the GitHub env vars
    aren't set, or if `requests` is missing — caller just gets False back.
    `repo` overrides which repo to push to (the DB backup uses this to
    target a private repo instead of the public GITHUB_REPO)."""
    target_repo = repo or GITHUB_REPO
    if not (REQUESTS_AVAILABLE and GITHUB_TOKEN and target_repo):
        return False

    content_b64 = base64.b64encode(content_bytes).decode("utf-8")
    api_url = f"https://api.github.com/repos/{target_repo}/contents/{repo_path}"
    headers = {
        "Authorization": f"token {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
    }

    sha = None
    try:
        resp = requests.get(api_url, headers=headers, params={"ref": GITHUB_BRANCH}, timeout=10)
        if resp.status_code == 200:
            sha = resp.json().get("sha")
    except Exception:
        logger.exception(f"Could not fetch existing sha for {repo_path} from GitHub")

    payload = {"message": commit_message, "content": content_b64, "branch": GITHUB_BRANCH}
    if sha:
        payload["sha"] = sha

    try:
        put_resp = requests.put(api_url, headers=headers, json=payload, timeout=20)
        if put_resp.status_code not in (200, 201):
            logger.warning("GitHub push to %s failed (%s): %s", repo_path, put_resp.status_code, put_resp.text)
            return False
        return True
    except Exception:
        logger.exception(f"Failed to push {repo_path} to GitHub")
        return False


def push_leaderboard_to_github():
    """Best-effort push of the current leaderboard to a GitHub repo file,
    so a static GitHub Pages site can read it. Off unless explicitly
    enabled — see ENABLE_GITHUB_DATA_SYNC above."""
    if not ENABLE_GITHUB_DATA_SYNC:
        return
    data = build_export_data()
    content_str = json.dumps(data, ensure_ascii=False, indent=2)
    _push_file_to_github(GITHUB_DATA_PATH, content_str.encode("utf-8"), "Update leaderboard data")


def backup_database_to_github():
    """Pushes the real SQLite database file itself (not just the
    leaderboard JSON) once a day — the actual data, including every
    user's Telegram ID, display name, batch, minutes, and self-declared
    Forest username, which otherwise lives ONLY on Railway's disk with no
    redundancy. Deliberately requires its OWN destination
    (BACKUP_GITHUB_REPO), separate from GITHUB_REPO — that repo is
    PUBLIC (needed for the website), so backing up real user data there
    would make it publicly downloadable. Off entirely until a private
    repo is explicitly configured, rather than silently falling back to
    the public one."""
    if not BACKUP_GITHUB_REPO:
        return
    if not os.path.exists(DB_PATH):
        return
    with open(DB_PATH, "rb") as f:
        db_bytes = f.read()
    ok = _push_file_to_github(
        "backups/forest-latest.db",
        db_bytes,
        f"Automated DB backup {local_today().isoformat()}",
        repo=BACKUP_GITHUB_REPO,
    )
    if ok:
        logger.info("Database backup pushed to GitHub")


async def job_backup_database(context: ContextTypes.DEFAULT_TYPE):
    backup_database_to_github()


def daily_total_all() -> int:
    today_str = local_today().isoformat()
    conn = db()
    row = conn.execute(
        _EFFECTIVE_CTE + "SELECT COALESCE(SUM(minutes),0) AS m FROM daily_effective",
        {"since": today_str},
    ).fetchone()
    conn.close()
    return row["m"]


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------


def default_display_name(user) -> str:
    """Leaderboards show Telegram @usernames, not real names — a deliberate
    privacy/tone choice, not a fallback. Only when someone has no public
    Telegram username do we fall back to their Telegram first name, and
    even then /setname lets them pick a handle-style name instead."""
    if user.username:
        return f"@{user.username}"
    return user.first_name


async def cmd_register(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/register Med25"""
    args = context.args
    user = update.effective_user
    if not args or args[0] not in VALID_BATCHES:
        await update.message.reply_text(
            "استخدم: /register Med25\n"
            f"الدفعات المتاحة: {', '.join(VALID_BATCHES)}"
        )
        return
    batch = args[0]
    is_founder = register_user(user.id, default_display_name(user), batch)
    founder_line = (
        f"\n🏅 أنت من أوائل روّاد {batch} — راح يظهر وسمك دايمًا!"
        if is_founder
        else ""
    )
    # Each on its own line, with an LRM before the @handle, so Telegram's
    # Arabic (RTL) rendering doesn't flip the @ to the wrong side of the
    # username — putting an LTR token mid-sentence in RTL text does that.
    await update.message.reply_text(
        f"✅ تم تسجيلك يا {user.first_name} ضمن دفعة {batch}!{founder_line}\n\n"
        f"📛 بالمتصدرين بيظهر اسمك كذا: \u200e{default_display_name(user)}\n"
        "تبي تغيّره لأي اسم ثاني؟ أرسل: /setname الاسم_الي_تبيه"
    )


async def cmd_setname(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Overrides the leaderboard name — mainly for people with no Telegram
    @username, or who want a different handle shown than their real one."""
    args = context.args
    user = update.effective_user
    if not get_user(user.id):
        await update.message.reply_text("سجّل نفسك أولًا بالأمر: /register Med25")
        return
    if not args:
        await update.message.reply_text("استخدم: /setname اليوزرنيم أو اللقب الي تبيه يظهر بالمتصدرين")
        return
    name = " ".join(args)
    set_display_name(user.id, name)
    await update.message.reply_text(f"✅ تم تحديث اسمك بالمتصدرين إلى: {name}")


async def cmd_setcollegehours(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/setcollegehours Sunday 08:00 14:00 — manager-only. Sets a
    recurring weekly window to exclude from that weekday's study
    schedule, since class timetables repeat by weekday rather than
    changing daily. /setcollegehours Sunday off clears a day."""
    user = update.effective_user
    if MANAGER_IDS and user.id not in MANAGER_IDS:
        await update.message.reply_text("هذا الأمر مخصص للمنظّمين فقط.")
        return
    args = context.args
    if len(args) < 2:
        await update.message.reply_text(
            "استخدم: /setcollegehours Sunday 08:00 14:00\n"
            "أو: /setcollegehours Sunday off  (لإلغاء يوم معيّن)\n"
            "الأيام: Sunday Monday Tuesday Wednesday Thursday Friday Saturday"
        )
        return
    day_name = args[0].lower()
    if day_name not in WEEKDAY_NAMES:
        await update.message.reply_text("اسم اليوم غلط. استخدم: Sunday, Monday, ... Saturday")
        return
    weekday = WEEKDAY_NAMES[day_name]
    day_ar = WEEKDAY_NAMES_AR[weekday]

    if args[1].lower() == "off":
        set_college_hours(weekday, None, None)
        await update.message.reply_text(f"✅ شيلنا استثناء الكلية ليوم {day_ar}.")
        return

    if len(args) != 3:
        await update.message.reply_text("استخدم: /setcollegehours Sunday 08:00 14:00")
        return
    start, end = args[1], args[2]
    try:
        sh, sm = map(int, start.split(":"))
        eh, em = map(int, end.split(":"))
        assert 0 <= sh < 24 and 0 <= sm < 60 and 0 <= eh < 24 and 0 <= em < 60
    except Exception:
        await update.message.reply_text("صيغة الوقت غلط. استخدم HH:MM مثل 08:00")
        return

    set_college_hours(weekday, start, end)
    await update.message.reply_text(
        f"✅ من الحين، جدول يوم {day_ar} بيتجنّب فترة الكلية من {start} إلى {end} تلقائيًا كل أسبوع."
    )


DONE_KEYWORDS = {"تم", "تمت", "خلص", "خلصت", "done", "Done", "DONE"}


def _save_schedule_state(sched: dict):
    """Persists the schedule state to the database — bot_data alone is
    in-memory only and gets wiped on every redeploy. Given how often this
    bot has redeployed, relying on memory for this silently breaks 'تم',
    link-detection, and /nextsession after any restart without any error
    showing anywhere — this is what actually makes those survive one."""
    try:
        serializable = {
            "date": sched["date"],
            "blocks": [[s.isoformat(), e.isoformat()] for s, e in sched["blocks"]],
            "gap_labels": {str(k): v for k, v in sched.get("gap_labels", {}).items()},
            "pinged": list(sched.get("pinged", set())),
            "done": list(sched.get("done", set())),
            "link_sent": list(sched.get("link_sent", set())),
        }
        set_setting("today_schedule_state", json.dumps(serializable, ensure_ascii=False))
    except Exception:
        logger.exception("Failed to persist today's schedule state")


def _load_schedule_state() -> dict | None:
    raw = get_setting("today_schedule_state")
    if not raw:
        return None
    try:
        data = json.loads(raw)
        blocks = [(datetime.fromisoformat(s), datetime.fromisoformat(e)) for s, e in data["blocks"]]
        return {
            "date": data["date"],
            "blocks": blocks,
            "gap_labels": {int(k): v for k, v in data.get("gap_labels", {}).items()},
            "pinged": set(data.get("pinged", [])),
            "done": set(data.get("done", [])),
            "link_sent": set(data.get("link_sent", [])),
        }
    except Exception:
        logger.exception("Failed to load persisted schedule state")
        return None


def _get_today_schedule(context) -> dict | None:
    """Prefers the fast in-memory cache, but transparently falls back to
    the database if the process restarted since the schedule was last
    touched — the whole point of this being persisted at all."""
    sched = context.application.bot_data.get("today_schedule")
    if sched and sched.get("date") == local_today().isoformat():
        return sched
    loaded = _load_schedule_state()
    if loaded and loaded["date"] == local_today().isoformat():
        context.application.bot_data["today_schedule"] = loaded
        return loaded
    return None


def _get_pinned_schedule_message_id(context):
    msg_id = context.application.bot_data.get("pinned_schedule_message_id")
    if msg_id:
        return msg_id
    stored = get_setting("pinned_schedule_message_id")
    if stored:
        msg_id = int(stored)
        context.application.bot_data["pinned_schedule_message_id"] = msg_id
        return msg_id
    return None


def _set_pinned_schedule_message_id(context, msg_id: int):
    context.application.bot_data["pinned_schedule_message_id"] = msg_id
    set_setting("pinned_schedule_message_id", str(msg_id))


async def _refresh_pinned_schedule(context):
    """Re-renders and re-saves the pinned schedule message in place —
    shared by the manual 'تم' handler and the automatic link-based
    completion job below, so both stay in sync the same way."""
    sched = _get_today_schedule(context)
    msg_id = _get_pinned_schedule_message_id(context)
    if not sched or not msg_id or not GROUP_CHAT_ID:
        return
    try:
        await context.bot.edit_message_text(
            chat_id=GROUP_CHAT_ID,
            message_id=msg_id,
            text=render_schedule_message(sched),
            parse_mode="HTML",
        )
    except Exception:
        logger.exception("Failed to edit the pinned schedule message")


async def handle_session_done(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """A manager typing 'تم' (or a few variants) in the sessions topic
    manually marks the most recently started, not-yet-completed study
    block as done. Manager-only, sessions topic only. This is the manual
    path — see job_auto_complete_sessions below for the automatic one."""
    user = update.effective_user
    if not update.message or not update.message.text:
        return
    if MANAGER_IDS and user.id not in MANAGER_IDS:
        return
    # Tolerant of trailing punctuation/emoji ("Done!", "تم ✅") — still
    # requires the keyword to be the whole message, not buried in a
    # sentence, so it can't misfire on unrelated chat.
    cleaned = update.message.text.strip().rstrip("!.؟? \t").strip("✅👍🔥🌲 ")
    if cleaned not in DONE_KEYWORDS:
        return
    if TOPIC_SESSIONS_ID and str(getattr(update.message, "message_thread_id", "")) != str(TOPIC_SESSIONS_ID):
        return

    sched = _get_today_schedule(context)
    if not sched:
        return

    now = local_now()
    done_set = sched.setdefault("done", set())
    candidates = [
        (start, end) for start, end in sched["blocks"]
        if start <= now and start.isoformat() not in done_set
    ]
    if not candidates:
        return
    start, end = max(candidates, key=lambda b: b[0])
    done_set.add(start.isoformat())
    _save_schedule_state(sched)
    await _refresh_pinned_schedule(context)


async def handle_suggestion_cooldown(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Enforces a per-user cooldown in the suggestions topic specifically
    — Telegram's own Slow Mode only applies group-wide (confirmed: this
    is an open feature request on Telegram's own tracker, not something
    that exists yet), so this is the bot doing it manually for just this
    one topic. A rapid-fire second message within the cooldown window
    gets deleted (requires the bot to have 'Delete Messages' admin
    rights) rather than just warned about, so the topic actually stays
    readable instead of filling with spam."""
    if not TOPIC_SUGGESTIONS_ID or not update.message:
        return
    if str(getattr(update.message, "message_thread_id", "")) != str(TOPIC_SUGGESTIONS_ID):
        return

    user = update.effective_user
    conn = db()
    row = conn.execute(
        "SELECT last_posted_at FROM suggestion_cooldowns WHERE user_id=?", (user.id,)
    ).fetchone()
    now = datetime.utcnow()
    if row:
        elapsed = (now - datetime.fromisoformat(row["last_posted_at"])).total_seconds()
        if elapsed < SUGGESTION_COOLDOWN_SECONDS:
            try:
                await update.message.delete()
                wait = int(SUGGESTION_COOLDOWN_SECONDS - elapsed)
                warning = await context.bot.send_message(
                    chat_id=update.effective_chat.id,
                    text=f"⏳ تمهّل شوي — تقدر ترسل اقتراح ثاني بعد {wait} ثانية.",
                    message_thread_id=update.message.message_thread_id,
                )
                # Auto-cleanup: delete the warning itself shortly after,
                # so the topic doesn't fill up with bot nag messages.
                context.job_queue.run_once(
                    lambda ctx: ctx.bot.delete_message(chat_id=warning.chat_id, message_id=warning.message_id),
                    when=8,
                )
            except Exception:
                logger.info("Could not enforce suggestion cooldown (check bot delete permission)")
            conn.close()
            return

    conn.execute(
        "INSERT INTO suggestion_cooldowns (user_id, last_posted_at) VALUES (?, ?) "
        "ON CONFLICT(user_id) DO UPDATE SET last_posted_at=excluded.last_posted_at",
        (user.id, now.isoformat()),
    )
    conn.commit()
    conn.close()


_URL_PATTERN = re.compile(r"https?://\S+")


async def handle_session_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """A manager posting any link in the sessions topic, during a
    currently-active block's time window, marks that block as
    'link_sent' — the signal job_auto_complete_sessions uses below to
    auto-checkmark it once the block's time ends, without anyone having
    to type 'تم' manually. If a link went out, someone almost certainly
    joined; no need to make an admin confirm that by hand too."""
    user = update.effective_user
    if not update.message or not update.message.text:
        return
    if MANAGER_IDS and user.id not in MANAGER_IDS:
        return
    if not _URL_PATTERN.search(update.message.text):
        return
    if TOPIC_SESSIONS_ID and str(getattr(update.message, "message_thread_id", "")) != str(TOPIC_SESSIONS_ID):
        return

    sched = _get_today_schedule(context)
    if not sched:
        return

    now = local_now()
    active = next((b for b in sched["blocks"] if b[0] <= now < b[1]), None)
    if not active:
        return
    sched.setdefault("link_sent", set()).add(active[0].isoformat())
    _save_schedule_state(sched)


async def job_auto_complete_sessions(context: ContextTypes.DEFAULT_TYPE):
    """Runs every 5 minutes alongside the ping job: any block whose time
    just ended, that had a link sent during it, and isn't already marked
    done, gets auto-checkmarked — no manual 'تم' needed for the common
    case where a link genuinely went out."""
    sched = _get_today_schedule(context)
    if not sched:
        return
    now = local_now()
    link_sent = sched.get("link_sent", set())
    done_set = sched.setdefault("done", set())
    changed = False
    for start, end in sched["blocks"]:
        key = start.isoformat()
        if end <= now < end + timedelta(minutes=5) and key in link_sent and key not in done_set:
            done_set.add(key)
            changed = True
    if changed:
        _save_schedule_state(sched)
        await _refresh_pinned_schedule(context)


async def cmd_checkschedule(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/checkschedule — identical to /testschedule, under a different name.
    Exists purely as a deployment diagnostic: if this shows the new
    labeled/spaced format while /testschedule still doesn't, that proves
    the new code IS live and something specific to the old command/old
    cached message is the problem — not a stuck deployment."""
    await cmd_testschedule(update, context)


async def cmd_postscheduletoday(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/postscheduletoday — manager-only. Triggers today's REAL schedule
    post right now, instead of waiting for tomorrow's automatic 00:05
    run. Respects the same enabled/paused toggle as the automatic post —
    if posting is still paused, this safely goes to managers privately
    instead of the group, same as always."""
    user = update.effective_user
    if MANAGER_IDS and user.id not in MANAGER_IDS:
        await update.message.reply_text("هذا الأمر مخصص للمنظّمين فقط.")
        return
    await job_post_daily_schedule(context)
    if get_setting("schedule_posting_enabled", "false") == "true":
        await update.message.reply_text("✅ تم نشر جدول اليوم بالقروب فعليًا.")
    else:
        await update.message.reply_text(
            "📩 النشر بالقروب موقوف حاليًا، فبعثت الجدول لكم خاص بدالها. "
            "فعّل النشر بـ /enablescheduleposts إذا تبيه ينزل بالقروب."
        )


async def cmd_enablescheduleposts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/enablescheduleposts — manager-only. Turns ON real group posting
    for the daily schedule (both the morning post and the per-block
    pings). Off by default — see job_post_daily_schedule."""
    user = update.effective_user
    if MANAGER_IDS and user.id not in MANAGER_IDS:
        await update.message.reply_text("هذا الأمر مخصص للمنظّمين فقط.")
        return
    set_setting("schedule_posting_enabled", "true")
    await update.message.reply_text(
        "✅ تفعّل نشر الجدول بالقروب. من أول جدول جاي (يدوي أو تلقائي)، بينشر بالقروب مباشرة."
    )


async def cmd_pausescheduleposts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/pausescheduleposts — manager-only. Puts group posting back to
    private-only mode, in case something needs re-testing later."""
    user = update.effective_user
    if MANAGER_IDS and user.id not in MANAGER_IDS:
        await update.message.reply_text("هذا الأمر مخصص للمنظّمين فقط.")
        return
    set_setting("schedule_posting_enabled", "false")
    await update.message.reply_text("⏸️ تم إيقاف نشر الجدول بالقروب مؤقتًا — بيرجع يوصلكم خاص بس.")


async def cmd_testschedule(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/testschedule [tomorrow|YYYY-MM-DD] — manager-only PRIVATE preview
    of a day's schedule, defaulting to today. Deliberately does NOT post
    to the group or touch the pinned message — with 100+ real members
    now, a test command that posts publicly is a real risk, not just a
    formatting inconvenience. This only replies to whoever ran the
    command, in whatever chat they ran it in."""
    user = update.effective_user
    if MANAGER_IDS and user.id not in MANAGER_IDS:
        await update.message.reply_text("هذا الأمر مخصص للمنظّمين فقط.")
        return

    target = local_today()
    arg_label = "اليوم"
    if context.args:
        arg = context.args[0].strip().lower()
        if arg in ("tomorrow", "غدا", "غدًا", "بكرة", "بكره"):
            target = target + timedelta(days=1)
            arg_label = "بكرة"
        else:
            try:
                target = date.fromisoformat(arg)
                arg_label = target.isoformat()
            except ValueError:
                await update.message.reply_text(
                    "استخدم: /testschedule أو /testschedule tomorrow أو /testschedule 2026-10-05"
                )
                return

    blocks, gap_labels = build_study_schedule(target)
    if not blocks:
        await update.message.reply_text(f"ما طلع جدول ليوم {arg_label} (تحقق من API أوقات الصلاة أو السجل).")
        return
    preview_sched = {"date": target.isoformat(), "blocks": blocks, "gap_labels": gap_labels, "done": set()}
    await update.message.reply_text(
        f"👁️ معاينة خاصة ليوم {arg_label} — ما انبعث بالقروب:\n\n" + render_schedule_message(preview_sched),
        parse_mode="HTML",
    )


async def cmd_setschedule(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/setschedule 13:00 22:00 — restricted to managers"""
    user = update.effective_user
    if MANAGER_IDS and user.id not in MANAGER_IDS:
        await update.message.reply_text("هذا الأمر مخصص لمنظّمي الجدول فقط.")
        return
    args = context.args
    if len(args) != 2:
        await update.message.reply_text("استخدم: /setschedule 13:00 22:00")
        return
    start, end = args
    text = f"📅 جدول اليوم:\n⏰ من {start} إلى {end}\nيلا بينا نزرع! 🌱"
    if GROUP_CHAT_ID:
        msg = await context.bot.send_message(chat_id=GROUP_CHAT_ID, text=text, **_topic_kwargs(TOPIC_SESSIONS_ID))
        try:
            await context.bot.pin_chat_message(chat_id=GROUP_CHAT_ID, message_id=msg.message_id)
        except Exception:
            pass
    else:
        await update.message.reply_text(text)


async def cmd_exammode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/exammode on|off — managers only. Softens the vibe during exam weeks:
    the website shows a banner and milestone posts pause their noise."""
    user = update.effective_user
    if MANAGER_IDS and user.id not in MANAGER_IDS:
        await update.message.reply_text("هذا الأمر مخصص لمنظّمي الجدول فقط.")
        return
    args = context.args
    if not args or args[0] not in ("on", "off"):
        await update.message.reply_text("استخدم: /exammode on أو /exammode off")
        return
    set_setting("exam_mode", args[0])
    if args[0] == "on":
        await update.message.reply_text(
            "📚 وضع الاختبارات مفعّل. خذوا وقتكم، والمذاكرة أهم من الترتيب هالفترة. بالتوفيق!"
        )
    else:
        await update.message.reply_text("✅ رجعنا للوضع العادي — يلا نكمل المنافسة!")


async def cmd_findpartner(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/findpartner رياضيات — finds someone else who logged the same tag
    recently. Prefers a same-batch match (same curriculum, most useful),
    but falls back to any batch rather than failing outright if nobody
    in-batch is currently active on that subject."""
    user = update.effective_user
    caller = get_user(user.id)
    if not caller:
        await update.message.reply_text("سجّل نفسك أولًا: /register Med25")
        return
    if not context.args:
        await update.message.reply_text("استخدم: /findpartner اسم المادة")
        return
    tag = " ".join(context.args)
    cutoff = (local_today() - timedelta(days=7)).isoformat()
    conn = db()

    row = conn.execute(
        """
        SELECT DISTINCT s.user_id, COALESCE(u.display_name, s.username) AS name, u.batch
        FROM sessions s
        LEFT JOIN users u ON u.user_id = s.user_id
        WHERE s.tag = ? AND s.session_date >= ? AND s.user_id != ? AND u.batch = ?
        LIMIT 1
        """,
        (tag, cutoff, user.id, caller["batch"]),
    ).fetchone()
    same_batch = row is not None

    if not row:
        row = conn.execute(
            """
            SELECT DISTINCT s.user_id, COALESCE(u.display_name, s.username) AS name, u.batch
            FROM sessions s
            LEFT JOIN users u ON u.user_id = s.user_id
            WHERE s.tag = ? AND s.session_date >= ? AND s.user_id != ?
            LIMIT 1
            """,
            (tag, cutoff, user.id),
        ).fetchone()
    conn.close()

    if not row:
        await update.message.reply_text(
            f"ما لقيت أحد يذاكر «{tag}» هالأسبوع. جرب لاحقًا أو غيّر المادة."
        )
        return

    batch_note = "" if same_batch else f" (من {row['batch']}، مو نفس دفعتك)"
    await update.message.reply_text(
        f"🤝 لقيت لك رفيق مذاكرة لمادة «{tag}»: {row['name']}{batch_note}\n"
        "راسله وابدأوا جلسة Plant Together!"
    )


async def cmd_log(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Manager-only manual correction: reply to the student's message with
    /log 90 to add/fix an entry. Regular logging is screenshot-only — see
    handle_photo below."""
    user = update.effective_user
    if not MANAGER_IDS or user.id not in MANAGER_IDS:
        await update.message.reply_text(
            "تسجيل الجلسات صار بالصورة فقط — صوّر شاشة اكتمال الجلسة في Forest وابعثها هنا 📸"
        )
        return
    if not update.message.reply_to_message:
        await update.message.reply_text("رد على رسالة الطالب بهذا الأمر: /log 90")
        return
    args = context.args
    if not args or not args[0].isdigit():
        await update.message.reply_text("استخدم: /log 90 (وأنت رادّ على رسالة الطالب)")
        return

    target = update.message.reply_to_message.from_user
    minutes = int(args[0])
    target_registered = get_user(target.id)
    if not target_registered:
        await update.message.reply_text("هذا الشخص ما سجّل نفسه بعد.")
        return

    log_session(target.id, target.username or target.full_name, minutes, "manager-correction")
    push_leaderboard_to_github()
    await broadcast_update()
    await update.message.reply_text(
        f"✅ تم تسجيل {minutes} دقيقة يدويًا لـ {target_registered['display_name']} (تصحيح من المنظم)."
    )


STRIKE_MESSAGES = {
    1: "⚠️ تنبيه لـ {name}: يرجى الانتباه خلال جلسات Plant Together — خروجك من الجلسة يفشّل الشجرة للجميع.",
    2: "⚠️⚠️ تنبيه ثانٍ لـ {name}: نفس الموضوع تكرر. يرجى الحرص أكثر بجلسات المذاكرة الجماعية.",
    3: "🚨 تنبيه أخير لـ {name}: هذا التنبيه الثالث. تكرار الموضوع راح يأثر على مشاركتك بالجلسات الجماعية.",
}


def find_users_by_name(name: str):
    """Case-insensitive partial match against EITHER registered
    display_name (Telegram) OR self-declared forest_username. The
    Forest-username match is honor-system only — nothing verifies it
    against Forest's actual data, since Forest has no public API at all.
    It's still useful: once someone self-declares it via /setforestname,
    an admin identifying them by their Forest name (e.g. from a
    screenshot) can resolve straight to their real Telegram account."""
    conn = db()
    name_clean = name.strip().lstrip("@").lower()
    rows = conn.execute(
        "SELECT user_id, display_name, forest_username FROM users "
        "WHERE LOWER(display_name) LIKE ? OR LOWER(COALESCE(forest_username, '')) LIKE ?",
        (f"%{name_clean}%", f"%{name_clean}%"),
    ).fetchall()
    conn.close()
    return rows


def set_forest_username(user_id: int, forest_username: str):
    conn = db()
    conn.execute("UPDATE users SET forest_username=? WHERE user_id=?", (forest_username, user_id))
    conn.commit()
    conn.close()


async def cmd_setforestname(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/setforestname <username> — self-service, any registered user can
    declare their own Forest app username. Purely self-reported — the
    bot has no way to verify this against Forest's actual data (no
    public API exists). Still useful: lets admins resolve a Forest
    username spotted in a screenshot back to the right Telegram account
    for things like /strike."""
    user = update.effective_user
    if not get_user(user.id):
        await update.message.reply_text("سجّل نفسك أولًا: /register Med25")
        return
    if not context.args:
        await update.message.reply_text("استخدم: /setforestname اسمك_بتطبيق_Forest")
        return
    forest_name = " ".join(context.args)
    set_forest_username(user.id, forest_name)
    await update.message.reply_text(
        f"✅ تم ربط يوزرنيم Forest «{forest_name}» بحسابك. "
        "ملاحظة: هذا بناءً على كلامك فقط — ما فيه طريقة نتأكد منه تقنيًا، بس يفيد لو احتجنا نربط اسمك بأي شي."
    )


def get_strike_count(user_id: int) -> int:
    conn = db()
    row = conn.execute("SELECT count FROM strikes WHERE user_id=?", (user_id,)).fetchone()
    conn.close()
    return row["count"] if row else 0


def add_strike(user_id: int) -> int:
    conn = db()
    conn.execute(
        "INSERT INTO strikes (user_id, count, last_strike_at) VALUES (?, 1, ?) "
        "ON CONFLICT(user_id) DO UPDATE SET count = count + 1, last_strike_at = excluded.last_strike_at",
        (user_id, datetime.utcnow().isoformat()),
    )
    conn.commit()
    row = conn.execute("SELECT count FROM strikes WHERE user_id=?", (user_id,)).fetchone()
    conn.close()
    return row["count"]


async def cmd_strike(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/strike — manager-only. Two ways to target someone: reply to
    their message, OR type /strike <name> to look them up by their
    registered display_name (the bot has no connection to Forest's
    identities — this only works if the name you know happens to match
    what they registered under on Telegram). Strikes 1-3 post an
    escalating public warning in the sessions topic. Strike 4+
    deliberately does NOT auto-punish anyone — it alerts managers
    privately instead, so a human decides what happens next rather than
    the bot silently restricting someone based on one admin's account of
    what happened."""
    user = update.effective_user
    if not MANAGER_IDS or user.id not in MANAGER_IDS:
        await update.message.reply_text("هذا الأمر مخصص للمنظّمين فقط.")
        return

    target_id = None
    name = None

    if update.message.reply_to_message:
        t = update.message.reply_to_message.from_user
        target_id, name = t.id, t.first_name
    elif context.args:
        query = " ".join(context.args)
        matches = find_users_by_name(query)
        if not matches:
            await update.message.reply_text(
                f"ما لقيت أحد مسجّل باسم قريب من «{query}».\n"
                "البوت ما يعرف أسماء Forest — بس الاسم المسجّل بالبوت (يوزرنيم تيليجرام عادةً). "
                "تأكد إن الشخص مسجّل أصلًا (/register)، أو جرّب اسم ثاني، أو رد على رسالة له لو متوفرة."
            )
            return
        if len(matches) > 1:
            names = "\n".join(f"- {m['display_name']}" for m in matches)
            await update.message.reply_text(f"لقيت أكثر من واحد يطابق:\n{names}\nحدد الاسم بالضبط.")
            return
        target_id, name = matches[0]["user_id"], matches[0]["display_name"]
    else:
        await update.message.reply_text(
            "استخدم: رد على رسالة الشخص بـ /strike، أو اكتب /strike اسمه (لازم يكون مسجّل بالبوت)."
        )
        return

    count = add_strike(target_id)

    if count in STRIKE_MESSAGES:
        text = STRIKE_MESSAGES[count].format(name=name)
        if GROUP_CHAT_ID:
            await context.bot.send_message(chat_id=GROUP_CHAT_ID, text=text, **_topic_kwargs(TOPIC_SESSIONS_ID))
    elif count >= 4:
        for mid in MANAGER_IDS:
            try:
                await context.bot.send_message(
                    chat_id=mid,
                    text=(
                        f"🚨 {name} وصل {count} تنبيهات بجلسات Plant Together.\n"
                        "القرار يرجع لكم — البوت ما يقدر يقيّد أحد تلقائيًا."
                    ),
                )
            except Exception:
                logger.info(f"Could not DM strike alert to manager {mid}")

    await update.message.reply_text(f"✅ تم تسجيل تنبيه رقم {count} لـ {name}.")


async def cmd_blockmember(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/blockmember — manager-only, explicit real moderation action.
    Reply to someone's message, or type /blockmember <name> to resolve
    via display_name or self-declared forest_username. Mutes them group-
    wide using Telegram's own restriction API (can still read, can't
    send) — Telegram's Bot API doesn't support muting in just one topic,
    only the whole group. This is DELIBERATELY never automatic, even at
    high strike counts — always a conscious command a manager types."""
    user = update.effective_user
    if not MANAGER_IDS or user.id not in MANAGER_IDS:
        await update.message.reply_text("هذا الأمر مخصص للمنظّمين فقط.")
        return
    if not GROUP_CHAT_ID:
        await update.message.reply_text("ما فيه GROUP_CHAT_ID معرّف.")
        return

    target_id = None
    name = None
    if update.message.reply_to_message:
        t = update.message.reply_to_message.from_user
        target_id, name = t.id, t.first_name
    elif context.args:
        query = " ".join(context.args)
        matches = find_users_by_name(query)
        if not matches:
            await update.message.reply_text(f"ما لقيت أحد مسجّل باسم قريب من «{query}».")
            return
        if len(matches) > 1:
            names = "\n".join(f"- {m['display_name']}" for m in matches)
            await update.message.reply_text(f"لقيت أكثر من واحد يطابق:\n{names}\nحدد الاسم بالضبط.")
            return
        target_id, name = matches[0]["user_id"], matches[0]["display_name"]
    else:
        await update.message.reply_text("استخدم: رد على رسالة الشخص بـ /blockmember، أو اكتب /blockmember اسمه.")
        return

    try:
        from telegram import ChatPermissions
        await context.bot.restrict_chat_member(
            chat_id=GROUP_CHAT_ID,
            user_id=target_id,
            permissions=ChatPermissions(can_send_messages=False),
        )
        await update.message.reply_text(
            f"🔇 تم كتم {name} بالقروب كامل (ملاحظة: تيليجرام ما يدعم الكتم بروم واحد بس، يكتم بالقروب كله). "
            "يُرفع الكتم بالأمر /unblockmember."
        )
    except Exception:
        logger.exception("Failed to restrict chat member")
        await update.message.reply_text(
            "⚠️ ما قدرت أكتمه — تأكد إن البوت أدمن بالقروب وله صلاحية تقييد الأعضاء."
        )


async def cmd_unblockmember(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/unblockmember — manager-only. Reverses /blockmember."""
    user = update.effective_user
    if not MANAGER_IDS or user.id not in MANAGER_IDS:
        await update.message.reply_text("هذا الأمر مخصص للمنظّمين فقط.")
        return
    if not GROUP_CHAT_ID:
        await update.message.reply_text("ما فيه GROUP_CHAT_ID معرّف.")
        return

    target_id = None
    name = None
    if update.message.reply_to_message:
        t = update.message.reply_to_message.from_user
        target_id, name = t.id, t.first_name
    elif context.args:
        query = " ".join(context.args)
        matches = find_users_by_name(query)
        if not matches:
            await update.message.reply_text(f"ما لقيت أحد مسجّل باسم قريب من «{query}».")
            return
        if len(matches) > 1:
            names = "\n".join(f"- {m['display_name']}" for m in matches)
            await update.message.reply_text(f"لقيت أكثر من واحد يطابق:\n{names}\nحدد الاسم بالضبط.")
            return
        target_id, name = matches[0]["user_id"], matches[0]["display_name"]
    else:
        await update.message.reply_text("استخدم: رد على رسالة الشخص بـ /unblockmember، أو اكتب /unblockmember اسمه.")
        return

    try:
        from telegram import ChatPermissions
        await context.bot.restrict_chat_member(
            chat_id=GROUP_CHAT_ID,
            user_id=target_id,
            permissions=ChatPermissions(
                can_send_messages=True,
                can_send_audios=True,
                can_send_documents=True,
                can_send_photos=True,
                can_send_videos=True,
                can_send_video_notes=True,
                can_send_voice_notes=True,
                can_send_polls=True,
                can_send_other_messages=True,
                can_add_web_page_previews=True,
            ),
        )
        await update.message.reply_text(f"🔊 تم رفع الكتم عن {name}.")
    except Exception:
        logger.exception("Failed to unrestrict chat member")
        await update.message.reply_text("⚠️ ما قدرت أرفع الكتم.")


async def cmd_strikes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/strikes — manager-only. Reply to someone's message, or type
    /strikes <name>, to check their current count without adding one."""
    user = update.effective_user
    if not MANAGER_IDS or user.id not in MANAGER_IDS:
        await update.message.reply_text("هذا الأمر مخصص للمنظّمين فقط.")
        return

    if update.message.reply_to_message:
        t = update.message.reply_to_message.from_user
        target_id, name = t.id, t.first_name
    elif context.args:
        query = " ".join(context.args)
        matches = find_users_by_name(query)
        if not matches:
            await update.message.reply_text(f"ما لقيت أحد مسجّل باسم قريب من «{query}».")
            return
        if len(matches) > 1:
            names = "\n".join(f"- {m['display_name']}" for m in matches)
            await update.message.reply_text(f"لقيت أكثر من واحد يطابق:\n{names}\nحدد الاسم بالضبط.")
            return
        target_id, name = matches[0]["user_id"], matches[0]["display_name"]
    else:
        await update.message.reply_text("رد على رسالة الشخص، أو اكتب /strikes اسمه.")
        return

    count = get_strike_count(target_id)
    await update.message.reply_text(f"📋 {name}: {count} تنبيه/تنبيهات مسجّلة.")


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    total = total_minutes(user.id)
    streak = current_streak(user.id)
    level = level_for_total(total)
    title = level_title_for(level)
    remaining = minutes_for_next_level(total)
    xp = total + streak * STREAK_XP_PER_DAY

    # Gap to this week's #1 — a concrete number to chase is usually more
    # motivating than an abstract rank number.
    week_start = week_start_for(local_today())
    week_rows = leaderboard(week_start, limit=1000)
    gap_line = ""
    if week_rows:
        top_total = week_rows[0]["total"]
        my_week_total = next((r["total"] for r in week_rows if r["user_id"] == user.id), 0)
        if my_week_total >= top_total and my_week_total > 0:
            gap_line = "\n👑 أنت الأول هالأسبوع — حافظ عليها!"
        elif top_total > 0:
            gap_line = f"\n🎯 تبعد {top_total - my_week_total} دقيقة عن المركز الأول هالأسبوع"

    # Same idea but for TODAY specifically — only shown when genuinely
    # close (<30 min), so it doesn't nag everyone who's far behind.
    today_rows = daily_top_achievers(local_today(), limit=1000)
    daily_gap_line = ""
    if today_rows:
        today_top = today_rows[0]["total"]
        my_today = next((r["total"] for r in today_rows if r["user_id"] == user.id), 0)
        daily_gap = today_top - my_today
        if 0 < daily_gap <= 30:
            daily_gap_line = f"\n🔥 بس {daily_gap} دقيقة وتكون الأول اليوم!"

    stats_registered = get_user(user.id)
    stats_display_name = stats_registered["display_name"] if stats_registered else user.first_name
    await update.message.reply_text(
        f"📊 إحصائياتك يا {stats_display_name}:\n"
        f"— المستوى {level} · {title}\n"
        f"— الإجمالي: {total} دقيقة ({total // 60} ساعة) — {xp} XP\n"
        f"— التتابع الحالي: {streak} يوم\n"
        f"— باقي {remaining} دقيقة للمستوى {level + 1}!"
        f"{gap_line}"
        f"{daily_gap_line}"
    )


async def cmd_leaderboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/leaderboard or /leaderboard Med25 for a single batch"""
    week_start = week_start_for(local_today())
    batch_filter = context.args[0] if context.args else None
    rows = leaderboard(week_start, batch=batch_filter)
    if not rows:
        await update.message.reply_text("لا توجد جلسات مسجّلة هذا الأسبوع بعد.")
        return
    medals = ["🥇", "🥈", "🥉"]
    title = f"🏆 ترتيب هذا الأسبوع"
    title += f" — {batch_filter}" if batch_filter else " (كل الدفعات)"
    lines = [title + "\n"]
    for i, r in enumerate(rows):
        prefix = medals[i] if i < 3 else f"{i+1}."
        batch_tag = f" [{r['batch']}]" if not batch_filter and r["batch"] else ""
        lines.append(f"{prefix} {r['name']}{batch_tag} — {r['total']} دقيقة")
    await update.message.reply_text("\n".join(lines))


async def check_and_announce_milestones(update, context, user):
    total = total_minutes(user.id)
    conn = db()
    for m in MILESTONES_MINUTES:
        if total >= m:
            already = conn.execute(
                "SELECT 1 FROM milestones_hit WHERE user_id=? AND milestone=?",
                (user.id, m),
            ).fetchone()
            if not already:
                conn.execute(
                    "INSERT INTO milestones_hit (user_id, milestone) VALUES (?, ?)",
                    (user.id, m),
                )
                conn.commit()
                if GROUP_CHAT_ID:
                    registered = get_user(user.id)
                    public_name = registered["display_name"] if registered else user.first_name
                    await context.bot.send_message(
                        chat_id=GROUP_CHAT_ID,
                        text=f"🎉 مبروك لـ \u200e{public_name}!\n{MILESTONE_LABELS[m]}",
                        **_topic_kwargs(TOPIC_ACHIEVEMENTS_ID),
                    )
    conn.close()


# ---------------------------------------------------------------------------
# Screenshot (OCR) handler
# ---------------------------------------------------------------------------


MONTH_ABBR = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
              "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def extract_minutes_from_ocr(text: str) -> int | None:
    """Parses a single Timeline entry's duration. Priority matters here:
    Forest's own phrasing ('75-minute Apple Tree') is unambiguous, so it's
    checked first. The MM:SS pattern is checked last and deliberately NOT
    first, because a completed entry also shows a start-end clock range
    like '07:12 - 08:28' that would otherwise be misread as the duration."""
    import re

    match = re.search(r"(\d{1,3})[\s-]*minutes?\b", text, re.IGNORECASE)
    if match:
        return int(match.group(1))
    # A running/paused timer display, e.g. "90:00"
    match = re.search(r"\b(\d{1,3}):00\b", text)
    if match:
        return int(match.group(1))
    match = re.search(r"\b(\d{2,3})\b", text)
    if match:
        return int(match.group(1))
    return None


def is_daily_card_screenshot(text: str) -> bool:
    """True for Forest's 'Focus Statistics' daily-overview card (the one
    with a Share button and a stamped date like '09.27 2026'), as opposed
    to a single Timeline session entry."""
    import re

    t = text.lower()
    if "focused time" in t or "focus statistics" in t or "focus trend" in t:
        return True
    return bool(re.search(r"\d{2}\.\d{2}\s*20\d{2}", text))


MAX_DAILY_CARD_MINUTES = int(os.environ.get("MAX_DAILY_CARD_MINUTES", "960"))
# 960 = 16 hours. 1440 (the literal 24h max) is technically possible but
# not realistic — nobody genuinely focus-studies for 18-23 real hours in
# one day, so a reading that high is almost certainly a misread. 16 hours
# leaves real headroom for an extreme marathon day while still catching
# clearly-impossible numbers like 1380. Adjust via the env var if needed.


def extract_daily_card_minutes(text: str) -> int | None:
    """Parses totals like '1 hour 25 minutes' or '1.4 hours' off the daily card."""
    import re

    m = re.search(r"(\d+)\s*hours?\s+(\d+)\s*minutes?", text, re.IGNORECASE)
    if m:
        return int(m.group(1)) * 60 + int(m.group(2))
    m = re.search(r"(\d+(?:\.\d+)?)\s*hours?", text, re.IGNORECASE)
    if m:
        return round(float(m.group(1)) * 60)
    m = re.search(r"(\d{1,3})\s*minutes?", text, re.IGNORECASE)
    if m:
        return int(m.group(1))
    return None


_MONTH_NAMES = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}


def extract_card_date(text: str) -> date | None:
    """Parses the daily card's stamped date. Unlike the Timeline heuristic
    below, this is precise enough to use as the actual session_date —
    which is what makes retroactive catch-up logging safe: the card names
    its own day, so the bot never has to assume 'today'.

    Forest shows the date in at least two different formats depending on
    the screen/version — '09.27 2026' (dotted numeric) on the Focus
    Statistics card, and 'Oct 2, 2026 (Today)' (month name) on the
    Overview screen — so both are tried."""
    import re

    m = re.search(r"(\d{2})\.(\d{2})\s*(20\d{2})", text)
    if m:
        month, day, year = int(m.group(1)), int(m.group(2)), int(m.group(3))
        try:
            return date(year, month, day)
        except ValueError:
            pass

    m = re.search(r"([A-Za-z]{3,9})\s+(\d{1,2}),?\s+(20\d{2})", text)
    if m:
        month = _MONTH_NAMES.get(m.group(1)[:3].lower())
        if month:
            try:
                return date(int(m.group(3)), month, int(m.group(2)))
            except ValueError:
                return None

    return None


def check_screenshot_date(text: str, today: date) -> str:
    """Weak heuristic, not proof: looks for today's day-of-month next to
    today's month abbreviation. Returns 'match', 'mismatch' (a different
    month abbreviation was found, suggesting an old/reused screenshot), or
    'unknown' (couldn't find a date at all — Forest's screen may not show
    one, so this never hard-blocks on its own)."""
    import re

    this_month = MONTH_ABBR[today.month - 1]
    day = str(today.day)
    if re.search(rf"{this_month}\D{{0,3}}{day}\b", text) or re.search(
        rf"\b{day}\D{{0,3}}{this_month}", text
    ):
        return "match"
    for abbr in MONTH_ABBR:
        if abbr != this_month and re.search(abbr, text):
            return "mismatch"
    return "unknown"


async def _ocr_and_prepare(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Downloads the photo, runs OCR + anti-cheat checks, and returns
    (minutes, image_hash, source, session_date) on success — source is
    'daily_card' for Forest's Focus Statistics screen (date-stamped, one
    per day, replaces that day's total — can be a PAST day, which is how
    catch-up logging works) or 'session' for a single Timeline entry
    (always logged under today). Sends an error reply and returns None on
    failure."""
    import hashlib

    if is_before_launch():
        await update.message.reply_text(
            f"📚 التسجيل الرسمي يبدأ {LAUNCH_DATE} — سجّل دفعتك الحين لأخذ وسم رائد الغابة 🏅، "
            "وارجع تصوّر جلساتك من يوم الإطلاق."
        )
        return None

    if not OCR_AVAILABLE:
        await update.message.reply_text(
            "قراءة الصور غير مفعّلة على هذا الخادم حاليًا — تواصل مع أحد المنظمين."
        )
        return None

    photo = update.message.photo[-1]
    file = await photo.get_file()
    buf = BytesIO()
    await file.download_to_memory(buf)
    raw = buf.getvalue()
    image_hash = hashlib.sha256(raw).hexdigest()

    if is_screenshot_used(image_hash):
        await update.message.reply_text(
            "⚠️ هذي الصورة مسجّلة من قبل — لازم صورة جديدة لكل تسجيل."
        )
        return None

    buf.seek(0)
    try:
        img = Image.open(buf)
        text = pytesseract.image_to_string(img)
    except Exception:
        # Logged server-side (visible in Railway's Deploy Logs) so a real
        # failure — e.g. the Tesseract engine missing — is distinguishable
        # from an actually blurry photo, instead of both looking identical
        # to the user.
        logger.exception("OCR failed while reading a submitted screenshot")
        await update.message.reply_text("ما قدرت أقرأ الصورة، صوّر شاشة أوضح وجرّب مرة ثانية.")
        return None

    today = local_today()

    if is_daily_card_screenshot(text):
        minutes = extract_daily_card_minutes(text)
        if minutes is None:
            await update.message.reply_text(
                "لقيت بطاقة الإحصائيات اليومية بس ما قدرت أقرأ الوقت الإجمالي بوضوح."
            )
            return None

        card_date = extract_card_date(text)
        if card_date is None:
            await update.message.reply_text(
                "ما قدرت أقرأ تاريخ البطاقة بوضوح. تأكد إن التاريخ أعلى البطاقة ظاهر بالصورة."
            )
            return None
        if card_date > today:
            await update.message.reply_text("⚠️ هذا تاريخ بالمستقبل! تأكد إنك صوّرت اليوم الصحيح.")
            return None
        if LAUNCH_DATE and card_date < date.fromisoformat(LAUNCH_DATE):
            await update.message.reply_text(
                f"⚠️ هذا التاريخ قبل بداية المسابقة الرسمية ({LAUNCH_DATE}) — ما يُحتسب."
            )
            return None
        if minutes > MAX_DAILY_CARD_MINUTES:
            # A day genuinely only has 1440 minutes — anything above that
            # is a misread (OCR grabbed the wrong number from the card),
            # not a real value. Reject rather than silently accepting an
            # impossible number that would show up publicly as a
            # "1,300-minute day," which is exactly what happened here.
            hours_cap = MAX_DAILY_CARD_MINUTES // 60
            await update.message.reply_text(
                f"⚠️ الرقم اللي قريته ({minutes} دقيقة) أكثر من {hours_cap} ساعة — رقم غير واقعي، على الأغلب قراءة غلط. "
                "صوّر البطاقة بوضوح أكثر وجرّب مرة ثانية. (لو فعلًا ذاكرت هالقدر، تواصل مع أحد الأدمنز)."
            )
            return None

        return minutes, image_hash, "daily_card", card_date

    minutes = extract_minutes_from_ocr(text)
    if minutes is None:
        await update.message.reply_text(
            "ما لقيت رقم واضح بالصورة. تأكد إن شاشة اكتمال الجلسة أو بطاقة اليوم كاملة وواضحة."
        )
        return None

    date_status = check_screenshot_date(text, today)
    if date_status == "mismatch":
        await update.message.reply_text(
            "⚠️ يبدو إن تاريخ الصورة مو تاريخ اليوم. جلسات Timeline تُسجَّل لليوم الحالي فقط — "
            "لو تبي تسجّل يوم فات، استخدم بطاقة ذاك اليوم من Overview بدالها."
        )
        return None

    return minutes, image_hash, "session", None


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user

    # Logging only happens in DM with the bot — keeps the group feed clean
    # (announcements, polls, milestones) instead of filling up with photo
    # confirmations. Redirect politely rather than silently ignoring it.
    if update.effective_chat.type != "private":
        bot_username = context.bot.username
        await update.message.reply_text(
            f"📩 سجّل من الخاص مع البوت مباشرة (@{bot_username})، مو هنا بالقروب — "
            "أبعث له الصورة هناك وبيردّ عليك."
        )
        return

    result = await _ocr_and_prepare(update, context)
    if result is None:
        return
    minutes, image_hash, source, session_date = result
    kind_label = "📊 بطاقة يوم" if source == "daily_card" else "📸 جلسة واحدة"
    day_note = ""
    if source == "daily_card" and session_date != local_today():
        day_note = f" (ليوم {session_date.isoformat()})"

    if not get_user(user.id):
        # Auto-registration: hold the OCR'd data and ask which batch they're in.
        context.user_data["pending_minutes"] = minutes
        context.user_data["pending_hash"] = image_hash
        context.user_data["pending_source"] = source
        context.user_data["pending_date"] = session_date.isoformat() if session_date else None
        keyboard = InlineKeyboardMarkup(
            [[InlineKeyboardButton(b, callback_data=f"regbatch:{b}")] for b in VALID_BATCHES]
        )
        await update.message.reply_text(
            f"{kind_label}{day_note}\n📸 وجدت *{minutes} دقيقة* بالصورة 👍\n\nقبل لا نسجّلها — وش دفعتك؟",
            reply_markup=keyboard,
            parse_mode="Markdown",
        )
        return

    context.user_data["pending_minutes"] = minutes
    context.user_data["pending_hash"] = image_hash
    context.user_data["pending_source"] = source
    context.user_data["pending_date"] = session_date.isoformat() if session_date else None
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ تأكيد", callback_data=f"confirm:{source}:{minutes}"),
                InlineKeyboardButton("✏️ مو صحيح", callback_data="edit"),
            ]
        ]
    )
    note = (
        "\n\nℹ️ بطاقة اليوم تحل محل أي جلسات سجّلتها لنفس اليوم، مو تضاف عليها"
        if source == "daily_card"
        else ""
    )
    await update.message.reply_text(
        f"{kind_label}{day_note}\n📸 وجدت *{minutes} دقيقة* — تأكيد؟{note}",
        reply_markup=keyboard,
        parse_mode="Markdown",
    )


async def _finalize_log(
    update, context, user, minutes: int, image_hash: str | None, source: str, session_date: date | None
):
    if source == "daily_card":
        log_daily_card(user.id, user.username or user.full_name, minutes, session_date)
    else:
        log_session(user.id, user.username or user.full_name, minutes, None)
    if image_hash:
        mark_screenshot_used(image_hash, user.id)
    await check_and_announce_milestones(update, context, user)
    await check_and_announce_streak_milestones(update, context, user)
    await check_and_announce_level_up(update, context, user)
    push_leaderboard_to_github()
    await broadcast_update()
    total = total_minutes(user.id)
    streak = current_streak(user.id)
    await update.callback_query.edit_message_text(
        "✅ *تم التسجيل بنجاح!*\n\n"
        f"📊 {minutes} دقيقة اليوم\n"
        f"💰 الرصيد الكلي: *{total} دقيقة*\n"
        f"🏅 المستوى: *Lv{level_for_total(total)} · {level_title_for(level_for_total(total))}*\n"
        f"🔥 الستريك: *{streak} يوم متتالي*",
        parse_mode="Markdown",
    )


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user = update.effective_user

    if query.data.startswith("regbatch:"):
        batch = query.data.split(":", 1)[1]
        is_founder = register_user(user.id, default_display_name(user), batch)
        minutes = context.user_data.get("pending_minutes")
        source = context.user_data.get("pending_source", "session")
        founder_line = f"\n🏅 وأنت من أوائل روّاد {batch}!" if is_founder else ""
        setname_hint = "\nℹ️ تقدر تغيّر اسمك بالمتصدرين بالأمر /setname"
        if minutes is None:
            await query.edit_message_text(
                f"✅ تم تسجيلك ضمن *{batch}*!{founder_line}{setname_hint}",
                parse_mode="Markdown",
            )
            return
        await query.edit_message_text(
            f"✅ تم تسجيلك ضمن *{batch}*!{founder_line}\n\n📸 وجدت *{minutes} دقيقة* بالصورة — تأكيد؟{setname_hint}",
            parse_mode="Markdown",
        )
        await context.bot.send_message(
            chat_id=query.message.chat_id,
            text="اضغط للتأكيد:",
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton("✅ تأكيد", callback_data=f"confirm:{source}:{minutes}"),
                        InlineKeyboardButton("✏️ مو صحيح", callback_data="edit"),
                    ]
                ]
            ),
        )

    elif query.data.startswith("confirm:"):
        _, source, minutes_str = query.data.split(":", 2)
        minutes = int(minutes_str)
        image_hash = context.user_data.get("pending_hash")
        date_str = context.user_data.get("pending_date")
        session_date = date.fromisoformat(date_str) if date_str else None
        if image_hash and is_screenshot_used(image_hash):
            await query.edit_message_text("⚠️ هذي الصورة اتسجّلت بالفعل.")
            return
        await _finalize_log(update, context, user, minutes, image_hash, source, session_date)
        context.user_data.pop("pending_minutes", None)
        context.user_data.pop("pending_hash", None)
        context.user_data.pop("pending_source", None)
        context.user_data.pop("pending_date", None)

    elif query.data == "edit":
        await query.edit_message_text(
            "تمام، صوّر شاشة أوضح — إما جلسة واحدة أو بطاقة اليوم الكاملة — وجرّب من جديد."
        )
        context.user_data.pop("pending_minutes", None)
        context.user_data.pop("pending_hash", None)
        context.user_data.pop("pending_source", None)
        context.user_data.pop("pending_date", None)


# ---------------------------------------------------------------------------
# Scheduled jobs
# ---------------------------------------------------------------------------


async def job_sync_website(context: ContextTypes.DEFAULT_TYPE):
    """Fallback safety-net sync, in case a per-log push ever fails silently."""
    push_leaderboard_to_github()
    await broadcast_update()


async def job_daily_summary(context: ContextTypes.DEFAULT_TYPE):
    if not GROUP_CHAT_ID:
        return
    today = local_today()
    total = daily_total_all()
    top = daily_top_achievers(today, limit=3)

    text = f"🌲 دقائق التركيز اليوم: {total}\n"
    if top:
        medals = ["🥇", "🥈", "🥉"]
        text += "\n👑 أبطال اليوم:\n"
        for i, r in enumerate(top):
            batch_tag = f" [{r['batch']}]" if r["batch"] else ""
            text += f"{medals[i]} {r['name']}{batch_tag} — {r['total']} دقيقة\n"
    text += "\nاستمروا، الغابة تكبر! 🍃"

    await context.bot.send_message(
        chat_id=GROUP_CHAT_ID,
        text=text,
        **_topic_kwargs(TOPIC_ACHIEVEMENTS_ID),
    )


PRAYER_CITY = os.environ.get("PRAYER_CITY", "Jeddah")
PRAYER_COUNTRY = os.environ.get("PRAYER_COUNTRY", "SaudiArabia")
PRAYER_METHOD = 4  # Umm al-Qura University, Makkah — correct for Saudi Arabia
PRAYER_BUFFER_BEFORE_MIN = 0  # gap starts exactly at the adhan, no early cutoff
PRAYER_BUFFER_AFTER_MIN = 40  # gap after, enough time to actually pray before the next block
MAGHRIB_BUFFER_AFTER_MIN = 30  # Maghrib specifically gets a shorter gap than the other prayers
JUMUAH_BUFFER_AFTER_MIN = 75  # Friday Dhuhr = Jumu'ah: khutbah + prayer runs much longer than a normal Dhuhr
MIN_SHORT_SESSION_MIN = 10  # leftover time shorter than this isn't worth its own session, just skip it
STUDY_BLOCK_MIN = 60
STUDY_BREAK_MIN = 10
# Saudi weekend (Fri/Sat) — no college, so fewer, longer blocks instead of
# the weekday rhythm. weekday(): Friday=4, Saturday=5.
WEEKEND_STUDY_BLOCK_MIN = 90
WEEKEND_STUDY_BREAK_MIN = 15


def fetch_prayer_times(for_date: date) -> dict | None:
    """Today's Jeddah prayer times from the free Aladhan API (no key
    needed). Returns None on any failure — callers degrade gracefully
    (skip the prayer-aware gaps) rather than crash a scheduled job over
    a flaky network call."""
    if not REQUESTS_AVAILABLE:
        return None
    try:
        resp = requests.get(
            "https://api.aladhan.com/v1/timingsByCity",
            params={
                "city": PRAYER_CITY,
                "country": PRAYER_COUNTRY,
                "method": PRAYER_METHOD,
                "date": for_date.strftime("%d-%m-%Y"),
            },
            timeout=10,
        )
        timings = resp.json()["data"]["timings"]
        result = {}
        for name in ("Fajr", "Dhuhr", "Asr", "Maghrib", "Isha"):
            hour, minute = map(int, timings[name].split(" ")[0].split(":"))
            result[name] = datetime.combine(for_date, dtime(hour=hour, minute=minute))
        return result
    except Exception:
        logger.exception("Failed to fetch prayer times")
        return None


def _ceil_5min(dt: datetime) -> datetime:
    """Rounds UP to the next clean 5-minute mark — safe for a block's
    START time, since it only ever delays it further, never earlier into
    whatever buffer it was placed after."""
    dt = dt.replace(second=0, microsecond=0)
    remainder = dt.minute % 5
    return dt if remainder == 0 else dt + timedelta(minutes=5 - remainder)


def _floor_5min(dt: datetime) -> datetime:
    """Rounds DOWN to the previous clean 5-minute mark — safe for a
    block's END time, since it only ever pulls it earlier, never later
    into whatever gap comes right after it."""
    dt = dt.replace(second=0, microsecond=0)
    return dt - timedelta(minutes=dt.minute % 5)


PRAYER_NAME_AR = {
    "Fajr": "الفجر", "Dhuhr": "الظهر", "Asr": "العصر", "Maghrib": "المغرب", "Isha": "العشاء",
}


def build_study_schedule(for_date: date) -> tuple[list[tuple[datetime, datetime]], dict[int, str]]:
    """Builds the day's study-block schedule (60 min study + 10 min break,
    repeating across the full day), skipping windows around each of the 5
    daily prayers so a block never lands mid-salah, plus any configured
    college hours. If the prayer API is unreachable, falls back to a plain
    back-to-back schedule rather than posting nothing at all.

    Returns (blocks, gap_labels) — gap_labels maps a block's index to the
    reason the block before it was skipped ("صلاة الفجر", "محاضرات", etc.),
    so the posted schedule can show WHY a gap exists instead of just a
    silent jump in times."""
    prayers = fetch_prayer_times(for_date)
    blocked = []
    if prayers:
        is_friday = for_date.weekday() == 4
        for name, t in prayers.items():
            if is_friday and name == "Dhuhr":
                after = JUMUAH_BUFFER_AFTER_MIN
            elif name == "Maghrib":
                after = MAGHRIB_BUFFER_AFTER_MIN
            else:
                after = PRAYER_BUFFER_AFTER_MIN
            label = "صلاة الجمعة" if (is_friday and name == "Dhuhr") else f"صلاة {PRAYER_NAME_AR[name]}"
            blocked.append((
                t - timedelta(minutes=PRAYER_BUFFER_BEFORE_MIN),
                t + timedelta(minutes=after),
                label,
            ))

    college = get_college_hours(for_date.weekday())
    if college:
        start_str, end_str = college
        sh, sm = map(int, start_str.split(":"))
        eh, em = map(int, end_str.split(":"))
        blocked.append((
            datetime.combine(for_date, dtime(hour=sh, minute=sm)),
            datetime.combine(for_date, dtime(hour=eh, minute=em)),
            "محاضرات",
        ))

    blocked.sort(key=lambda b: b[0])

    is_weekend = for_date.weekday() in (4, 5)  # Friday, Saturday
    block_min = WEEKEND_STUDY_BLOCK_MIN if is_weekend else STUDY_BLOCK_MIN
    break_min = WEEKEND_STUDY_BREAK_MIN if is_weekend else STUDY_BREAK_MIN

    day_start = datetime.combine(for_date, dtime(hour=0, minute=0))
    day_end = datetime.combine(for_date, dtime(hour=23, minute=59))

    # cursor is kept exactly 5-minute-aligned at all times — day_start
    # (00:00) already is, block_min/break_min are both multiples of 5, and
    # every place cursor jumps past a blocked window below rounds that
    # jump to a clean mark too. That's what keeps the WHOLE day's grid
    # exact, instead of rounding each block independently after the fact
    # (which drifts: a later block's displayed gap can silently stop
    # matching the real 10/15-min break, with no label explaining why).
    blocks = []
    gap_labels = {}
    pending_label = None
    cursor = day_start
    while cursor < day_end:
        block_end = cursor + timedelta(minutes=block_min)
        overlap = next((b for b in blocked if cursor < b[1] and block_end > b[0]), None)
        if overlap:
            # A full-length block doesn't fit before this blocked window —
            # but don't just lose whatever time IS left before it. If
            # there's enough room for a genuinely useful shorter session,
            # insert one instead of skipping straight to the window's end.
            short_end = _floor_5min(overlap[0])  # never later than the real prayer start
            short_duration = (short_end - cursor).total_seconds() / 60
            if short_duration >= MIN_SHORT_SESSION_MIN:
                if pending_label:
                    gap_labels[len(blocks)] = pending_label
                    pending_label = None
                blocks.append((cursor, short_end))
            pending_label = overlap[2]
            cursor = _ceil_5min(overlap[1])  # resets the grid cleanly — never earlier than safe
            continue
        if block_end > day_end:
            break
        if pending_label:
            gap_labels[len(blocks)] = pending_label
            pending_label = None
        blocks.append((cursor, block_end))
        cursor = block_end + timedelta(minutes=break_min)
    return blocks, gap_labels


def render_schedule_message(sched: dict) -> str:
    """Builds the schedule post's text from stored state — including
    prayer/college gap labels and any blocks a manager has marked done
    (shown struck through with a checkmark). HTML parse mode, since
    Telegram's legacy Markdown doesn't support strikethrough."""
    lines = [
        "🌲 <b>جدول المذاكرة الجماعي اليوم</b>",
        "",
        "أي فترة، اللي يذاكر وقتها من الأدمنز يسوي Plant Together بتطبيق Forest ويبعث رابط الانضمام هنا 🔗",
        "",
    ]
    done_set = sched.get("done", set())
    gap_labels = sched.get("gap_labels", {})
    for i, (start, end) in enumerate(sched["blocks"]):
        label = gap_labels.get(i)
        if label:
            icon = "🕌" if "صلاة" in label else "📚"
            lines.append(f"{icon} <i>{label}</i>")
            lines.append("")
        line = f"⏰ {start.strftime('%I:%M %p')} – {end.strftime('%I:%M %p')}"
        if start.isoformat() in done_set:
            lines.append(f"✅ <s>{line}</s>")
        else:
            lines.append(line)
        lines.append("")  # a blank line after EVERY session, not just around gap labels
    return "\n".join(lines).rstrip()


STREAK_BREAK_THRESHOLD = 7  # only nudge for a streak that was actually meaningful


async def job_check_streak_breaks(context: ContextTypes.DEFAULT_TYPE):
    """Once a day, just after midnight: finds anyone whose real streak
    (7+ days) broke yesterday, and sends ONE gentle, private nudge —
    never public, since a broken streak is nobody else's business and
    public callouts would undermine the whole feature."""
    today = local_today()
    yesterday = today - timedelta(days=1)
    day_before = yesterday - timedelta(days=1)

    conn = db()
    users = conn.execute("SELECT user_id FROM users").fetchall()
    conn.close()

    for u in users:
        was_active_yesterday = current_streak(u["user_id"], as_of=yesterday) > 0
        if was_active_yesterday:
            continue
        prior_streak = current_streak(u["user_id"], as_of=day_before)
        if prior_streak >= STREAK_BREAK_THRESHOLD:
            try:
                await context.bot.send_message(
                    chat_id=u["user_id"],
                    text=(
                        f"💭 لاحظنا انقطاع ستريكك بعد {prior_streak} يوم متتالي.\n"
                        "يصير، المهم ترجع تبدأ من جديد 🌱"
                    ),
                )
            except Exception:
                logger.info(f"Could not DM streak-break nudge to user {u['user_id']}")


async def job_post_daily_schedule(context: ContextTypes.DEFAULT_TYPE):
    """Posts the full day's study-block schedule once each morning. Each
    block is open hosting, not an assigned shift — whoever happens to be
    studying with Forest at that time just taps Plant Together and shares
    the join link, since they're already there."""
    if not GROUP_CHAT_ID:
        return
    today = local_today()
    blocks, gap_labels = build_study_schedule(today)
    sched = {
        "date": today.isoformat(),
        "blocks": blocks,
        "gap_labels": gap_labels,
        "pinged": set(),
        "done": set(),
    }
    context.application.bot_data["today_schedule"] = sched
    _save_schedule_state(sched)
    if not blocks:
        return

    rendered = render_schedule_message(sched)

    # Kill-switch: while this is "off", even the AUTOMATIC daily post goes
    # privately to managers instead of the real group — group has 100+
    # real members now, so nothing schedule-related touches it publicly
    # until a manager explicitly confirms it's safe via /enablescheduleposts.
    if get_setting("schedule_posting_enabled", "false") != "true":
        for mid in MANAGER_IDS:
            try:
                await context.bot.send_message(
                    chat_id=mid,
                    text="⚠️ نشر الجدول بالقروب موقوف حاليًا (وضع الاختبار). هذا اللي كان بينشر:\n\n" + rendered,
                    parse_mode="HTML",
                )
            except Exception:
                logger.info(f"Could not DM schedule preview to manager {mid}")
        return

    msg = await context.bot.send_message(
        chat_id=GROUP_CHAT_ID,
        text=rendered,
        parse_mode="HTML",
        **_topic_kwargs(TOPIC_SESSIONS_ID),
    )

    # Unpin yesterday's message first — in its OWN try/except, since a
    # stale/invalid old message_id (e.g. from before a group migrated to
    # a supergroup) must never block pinning today's message or updating
    # which message_id is "current" below.
    old_message_id = _get_pinned_schedule_message_id(context)
    if old_message_id:
        try:
            await context.bot.unpin_chat_message(chat_id=GROUP_CHAT_ID, message_id=old_message_id)
        except Exception:
            pass  # most likely just a stale reference — harmless either way

    try:
        await context.bot.pin_chat_message(chat_id=GROUP_CHAT_ID, message_id=msg.message_id, disable_notification=True)
    except Exception:
        # Most likely cause: the bot isn't an admin, or lacks pin permission.
        # Not worth failing the whole job over — the schedule still posted.
        logger.warning("Could not pin the daily schedule message (check bot admin rights)")

    # Always update this, pin succeeded or not — the "تم" done-marking
    # feature needs to know which message is current regardless.
    _set_pinned_schedule_message_id(context, msg.message_id)


async def cmd_nextsession(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/nextsession — shows the next upcoming study block without needing
    to scroll back up to the pinned morning post."""
    sched = _get_today_schedule(context)
    if not sched:
        await update.message.reply_text("ما فيه جدول لليوم بعد — ينشر كل يوم الساعة 12:05 صباحًا.")
        return
    now = local_now()
    upcoming = next((b for b in sched["blocks"] if b[1] > now), None)
    if not upcoming:
        await update.message.reply_text("خلصت فترات اليوم 🌙 ترقب جدول بكرة.")
        return
    start, end = upcoming
    status = "جارية الحين 🟢" if start <= now else "قادمة"
    await update.message.reply_text(
        f"⏰ الفترة القادمة ({status}):\n{start.strftime('%I:%M %p')} – {end.strftime('%I:%M %p')}"
    )


async def job_check_schedule_pings(context: ContextTypes.DEFAULT_TYPE):
    """Runs every 5 minutes; fires one short heads-up 5 minutes BEFORE
    each scheduled block starts — not at the start itself. A ping at the
    start is redundant when an admin already shared the link ahead of
    time (the normal case); a heads-up beforehand is what's actually
    useful, whether or not a link's already out."""
    if not GROUP_CHAT_ID:
        return
    if get_setting("schedule_posting_enabled", "false") != "true":
        return  # same kill-switch as the morning post — stay quiet in the group until enabled
    sched = _get_today_schedule(context)
    if not sched:
        return
    now = local_now()
    changed = False
    for start, end in sched["blocks"]:
        ping_time = start - timedelta(minutes=5)
        if ping_time <= now < start and start.isoformat() not in sched["pinged"]:
            sched["pinged"].add(start.isoformat())  # string, not datetime — keeps it JSON-serializable
            changed = True
            await context.bot.send_message(
                chat_id=GROUP_CHAT_ID,
                text=(
                    f"🌲 تبدأ فترة مذاكرة بعد ٥ دقايق ({start.strftime('%I:%M %p')} – {end.strftime('%I:%M %p')})\n"
                    "لو ما فيه رابط بعد، أحد الأدمنز يبدأ Plant Together ويشاركه هنا."
                ),
                **_topic_kwargs(TOPIC_SESSIONS_ID),
            )
    if changed:
        _save_schedule_state(sched)


DAILY_POLL_QUESTION = "⏱️ كم ساعة ذاكرت اليوم؟"
DAILY_POLL_OPTIONS = ["لسا ما بدأت 😅", "أقل من ساعة", "1-2 ساعة", "3-4 ساعات", "5+ ساعات 🔥"]


async def job_daily_poll(context: ContextTypes.DEFAULT_TYPE):
    if not GROUP_CHAT_ID:
        return
    await context.bot.send_poll(
        chat_id=GROUP_CHAT_ID,
        question=DAILY_POLL_QUESTION,
        options=DAILY_POLL_OPTIONS,
        is_anonymous=False,
        **_topic_kwargs(TOPIC_CHAT_ID),
    )


async def cmd_testpoll(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/testpoll — manager-only PRIVATE preview of the daily poll. Sends
    a real, functioning poll to the manager's own DM (polls work fine in
    private chats) instead of the group — same private-testing pattern
    as /testschedule, for the same reason: nothing untested touches the
    group with 100+ real members in it."""
    user = update.effective_user
    if MANAGER_IDS and user.id not in MANAGER_IDS:
        await update.message.reply_text("هذا الأمر مخصص للمنظّمين فقط.")
        return
    await context.bot.send_poll(
        chat_id=user.id,
        question=DAILY_POLL_QUESTION,
        options=DAILY_POLL_OPTIONS,
        is_anonymous=False,
    )


async def job_weekly_personal_recap(context: ContextTypes.DEFAULT_TYPE):
    """DMs every registered user their own recap of the week that just
    ended — separate from the public leaderboard post, so people get a
    personal nudge even if they didn't crack the top of the group message.
    Runs Sunday morning, after Saturday night's public leaderboard."""
    today = local_today()
    this_week_start = week_start_for(today)
    last_week_start = this_week_start - timedelta(days=7)

    rows = leaderboard(last_week_start, limit=10000)
    if not rows:
        return
    totals_by_user = {r["user_id"]: r["total"] for r in rows}
    top_total = rows[0]["total"]

    conn = db()
    users = conn.execute("SELECT user_id, display_name, batch FROM users").fetchall()
    conn.close()

    for u in users:
        minutes = totals_by_user.get(u["user_id"], 0)
        if minutes == 0:
            continue  # no activity last week — skip the nudge, not a guilt trip
        streak = current_streak(u["user_id"])
        if minutes >= top_total:
            gap_line = "👑 كنت الأول بين الكل الأسبوع الماضي!"
        else:
            gap_line = f"🎯 كنت تبعد {top_total - minutes} دقيقة عن المركز الأول"
        try:
            await context.bot.send_message(
                chat_id=u["user_id"],
                text=(
                    "📬 *ملخصك الأسبوعي*\n\n"
                    f"⏱️ ذاكرت {minutes} دقيقة الأسبوع اللي فات\n"
                    f"🔥 الستريك الحالي: {streak} يوم\n"
                    f"{gap_line}"
                ),
                parse_mode="Markdown",
            )
        except Exception:
            # Most common cause: the user blocked the bot or never started
            # a DM with it — not worth failing the whole batch over.
            logger.info(f"Could not DM weekly recap to user {u['user_id']}")


async def job_weekly_leaderboard(context: ContextTypes.DEFAULT_TYPE):
    if not GROUP_CHAT_ID:
        return
    today = local_today()
    week_start = week_start_for(today)
    last_week_start = week_start - timedelta(days=7)

    rows = leaderboard(week_start)
    if rows:
        winner = rows[0]
        record_hall_of_fame(
            week_start, winner["user_id"], winner["name"], winner["batch"], winner["total"]
        )

    medals = ["🥇", "🥈", "🥉"]
    lines = ["🏆 نتيجة هذا الأسبوع (كل الدفعات):\n"]
    for i, r in enumerate(rows):
        prefix = medals[i] if i < 3 else f"{i+1}."
        batch_tag = f" [{r['batch']}]" if r["batch"] else ""
        lines.append(f"{prefix} {r['name']}{batch_tag} — {r['total']} دقيقة")

    best_user, delta = most_improved(week_start, last_week_start)
    if best_user and delta > 0:
        lines.append(f"\n📈 الأكثر تحسنًا: {best_user} (+{delta} دقيقة عن الأسبوع الماضي)")

    # Per-batch breakdown, only for batches with any activity this week
    active_batches = batches_with_activity(week_start)
    for batch in active_batches:
        batch_rows = leaderboard(week_start, limit=3, batch=batch)
        if not batch_rows:
            continue
        lines.append(f"\n🎓 أفضل 3 في {batch}:")
        for i, r in enumerate(batch_rows):
            prefix = medals[i] if i < 3 else f"{i+1}."
            lines.append(f"{prefix} {r['name']} — {r['total']} دقيقة")

    # Batch-vs-batch: both the raw total (recruitment incentive) and the
    # per-capita average (fairness), for every batch with at least one
    # registered member.
    registered_batches = [b for b in VALID_BATCHES if batch_member_count(b) > 0]
    if len(registered_batches) > 1:
        totals = batch_totals(week_start)
        averages = batch_averages(week_start)
        ranked = sorted(registered_batches, key=lambda b: totals.get(b, 0), reverse=True)
        lines.append("\n⚔️ حرب الدفعات (إجمالي / متوسط الفرد):")
        for b in ranked:
            lines.append(f"— {b}: {totals.get(b, 0)} د إجمالي، {averages.get(b, 0)} د/فرد")
        last_batch = ranked[-1]
        lines.append(f"\n⚠️ {last_batch} في آخر الترتيب هذا الأسبوع — ادعوا أصحابكم قبل لا تخسرون!")

    await context.bot.send_message(
        chat_id=GROUP_CHAT_ID, text="\n".join(lines), **_topic_kwargs(TOPIC_ACHIEVEMENTS_ID)
    )


async def job_monthly_recap(context: ContextTypes.DEFAULT_TYPE):
    if not GROUP_CHAT_ID:
        return
    today = local_today()
    month_start = today.replace(day=1)
    conn = db()
    total = conn.execute(
        "SELECT COALESCE(SUM(minutes),0) AS m FROM sessions WHERE session_date >= ?",
        (month_start.isoformat(),),
    ).fetchone()["m"]
    best_day = conn.execute(
        "SELECT session_date, SUM(minutes) AS m FROM sessions WHERE session_date >= ? "
        "GROUP BY session_date ORDER BY m DESC LIMIT 1",
        (month_start.isoformat(),),
    ).fetchone()
    conn.close()

    text = f"📅 ملخص الشهر:\n— إجمالي الدقائق: {total} ({total // 60} ساعة)\n"
    if best_day:
        text += f"— أكثر يوم نشاطًا: {best_day['session_date']} ({best_day['m']} دقيقة)\n"
    await context.bot.send_message(chat_id=GROUP_CHAT_ID, text=text, **_topic_kwargs(TOPIC_ACHIEVEMENTS_ID))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    if not BOT_TOKEN:
        raise SystemExit("Set the BOT_TOKEN environment variable first.")

    init_db()

    async def post_init(app: Application):
        await start_ws_server(app)

    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()

    app.add_handler(CommandHandler("register", cmd_register))
    app.add_handler(CommandHandler("setname", cmd_setname))
    app.add_handler(CommandHandler("setschedule", cmd_setschedule))
    app.add_handler(CommandHandler("testschedule", cmd_testschedule))
    app.add_handler(CommandHandler("checkschedule", cmd_checkschedule))
    app.add_handler(CommandHandler("postscheduletoday", cmd_postscheduletoday))
    app.add_handler(CommandHandler("testpoll", cmd_testpoll))
    app.add_handler(CommandHandler("enablescheduleposts", cmd_enablescheduleposts))
    app.add_handler(CommandHandler("pausescheduleposts", cmd_pausescheduleposts))
    app.add_handler(CommandHandler("setcollegehours", cmd_setcollegehours))
    app.add_handler(CommandHandler("exammode", cmd_exammode))
    app.add_handler(CommandHandler("findpartner", cmd_findpartner))
    app.add_handler(CommandHandler("log", cmd_log))
    app.add_handler(CommandHandler("strike", cmd_strike))
    app.add_handler(CommandHandler("strikes", cmd_strikes))
    app.add_handler(CommandHandler("setforestname", cmd_setforestname))
    app.add_handler(CommandHandler("blockmember", cmd_blockmember))
    app.add_handler(CommandHandler("unblockmember", cmd_unblockmember))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("nextsession", cmd_nextsession))
    app.add_handler(CommandHandler("leaderboard", cmd_leaderboard))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_session_done))
    # group=1: both this and the "تم" handler above match the same TEXT
    # filter, and a single group only runs the first match by default —
    # a different group makes BOTH actually process every text message.
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_session_link), group=1)
    # group=2: catches ANY message type (text, photos, etc.) in the
    # suggestions topic specifically — separate group so it runs
    # independently of the other two text handlers above.
    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, handle_suggestion_cooldown), group=2)
    app.add_handler(CallbackQueryHandler(handle_callback))

    # Schedule automated posts (times are local per TZ_OFFSET_HOURS, expressed as UTC here)
    jq = app.job_queue
    # Daily summary at 23:00 local
    jq.run_daily(job_daily_summary, time=dtime(hour=(23 - TZ_OFFSET_HOURS) % 24))
    # Daily "which tree" poll at 17:00 local
    jq.run_daily(job_daily_poll, time=dtime(hour=(17 - TZ_OFFSET_HOURS) % 24))
    # Weekly leaderboard every Saturday at 23:30 local (weekday 5 = Saturday)
    jq.run_daily(
        job_weekly_leaderboard,
        time=dtime(hour=(23 - TZ_OFFSET_HOURS) % 24, minute=30),
        days=(5,),
    )
    # Monthly recap on the 1st at 09:00 local
    jq.run_monthly(job_monthly_recap, when=dtime(hour=(9 - TZ_OFFSET_HOURS) % 24), day=1)
    # Personal weekly recap, DM'd individually, Sunday 10:00 local (weekday 6 = Sunday)
    jq.run_daily(
        job_weekly_personal_recap,
        time=dtime(hour=(10 - TZ_OFFSET_HOURS) % 24),
        days=(6,),
    )
    # Safety-net website sync every 30 minutes, in case a per-log push fails
    jq.run_repeating(job_sync_website, interval=1800, first=60)
    # Full database backup to GitHub once a day at 04:00 local (quiet hour)
    jq.run_daily(job_backup_database, time=dtime(hour=(4 - TZ_OFFSET_HOURS) % 24))
    # Post the day's prayer-aware study-block schedule once, early each morning
    jq.run_daily(job_post_daily_schedule, time=dtime(hour=(0 - TZ_OFFSET_HOURS) % 24, minute=5))
    # Gentle private nudge for anyone whose 7+ day streak just broke
    jq.run_daily(job_check_streak_breaks, time=dtime(hour=(0 - TZ_OFFSET_HOURS) % 24, minute=15))
    # Check every 5 minutes whether a scheduled block just started
    jq.run_repeating(job_check_schedule_pings, interval=300, first=30)
    jq.run_repeating(job_auto_complete_sessions, interval=300, first=45)

    logger.info("Bot starting...")
    app.run_polling()


if __name__ == "__main__":
    main()
