"""Offline tests for forest_bot.py. Runs the real bot code against a temporary
SQLite DB with a fake Telegram bot — nothing is ever sent to Telegram.

Run:  python3 tests/test_bot_offline.py
(optional 2nd arg: path to a copy of an OLD database, to test migrations)"""
import asyncio, os, sys, json, random, tempfile, shutil, sqlite3, types
from datetime import date, timedelta, datetime

REPO = sys.argv[1] if len(sys.argv) > 1 else os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OLD_DB = sys.argv[2] if len(sys.argv) > 2 else None
tmp = tempfile.mkdtemp()
os.environ.update(DB_PATH=os.path.join(tmp, "t.db"), BOT_TOKEN="x", GROUP_CHAT_ID="-100123",
                  TZ_OFFSET_HOURS="3", MANAGER_IDS="1", TOPIC_ACHIEVEMENTS_ID="7")
sys.path.insert(0, REPO)
import forest_bot as fb

results = []
def check(name, cond, detail=""):
    results.append(bool(cond))
    print(("PASS " if cond else "FAIL ") + name + (f"  — {detail}" if detail else ""))

class FakeBot:
    def __init__(self): self.sent = []
    async def send_message(self, chat_id, text, **kw): self.sent.append((chat_id, text, kw))
    async def send_photo(self, chat_id, photo, **kw): self.sent.append((chat_id, "<photo>", kw))
    async def send_poll(self, chat_id, question, options, **kw): self.sent.append((chat_id, question, kw))
ctx = lambda: types.SimpleNamespace(bot=FakeBot(), bot_data={}, user_data={}, args=[])

# ---- 1. migration on a DB with the OLD production schema -------------
if OLD_DB:
    shutil.copy(OLD_DB, os.environ["DB_PATH"])
    before = sqlite3.connect(OLD_DB).execute("select count(*) from sessions").fetchone()[0]
    fb.init_db(); fb.init_db()  # twice: must be idempotent
    c = sqlite3.connect(os.environ["DB_PATH"])
    tables = {r[0] for r in c.execute("select name from sqlite_master where type='table'")}
    after = c.execute("select count(*) from sessions").fetchone()[0]
    check("migration adds new tables on old DB",
          {"monthly_titles", "streak_nudges_sent", "session_time_windows"} <= tables)
    check("migration keeps every existing session", before == after, f"{before} -> {after}")
    c.close()
    os.remove(os.environ["DB_PATH"])

# ---- 2. fresh DB with synthetic members -----------------------------
fb.init_db()
today = fb.local_today()
random.seed(1)
users = [(1001, "@sara", "Med25"), (1002, "@omar", "Med26"), (1003, "@lina", "Med25")]
for uid, name, batch in users:
    fb.register_user(uid, name, batch)
conn = fb.db()
for uid, _, _ in users:
    for d in range(70):
        day = today - timedelta(days=d)
        if uid == 1003 and d < 1:  # lina: 10-day streak through yesterday, nothing today
            continue
        if uid == 1003 and d > 10:
            continue
        mins = random.randint(30, 240) if uid != 1002 or d % 3 else 0
        if mins:
            conn.execute("INSERT INTO sessions (user_id, username, minutes, tag, logged_at, session_date, source) "
                         "VALUES (?,?,?,?,?,?,?)", (uid, "u", mins, "Anatomy " if d % 2 else "anatomy",
                         datetime.combine(day, datetime.min.time()).replace(hour=random.randint(8, 23)).isoformat(),
                         day.isoformat(), "session"))
conn.commit(); conn.close()

# ---- 3. tag normalization ---------------------------------------------
check("_normalize_tag lowercases/trims", fb._normalize_tag("  Anatomy ") == "anatomy")
fb.log_session(1001, "u", 25, "  PHYSIOLOGY")
row = fb.db().execute("select tag from sessions order by id desc limit 1").fetchone()
check("log_session stores normalized tag", row["tag"] == "physiology", row["tag"])

# ---- 4. personal analytics + heatmap ----------------------------------
a = fb.build_personal_analytics(1001)
check("build_personal_analytics returns data", isinstance(a, dict) and a, ", ".join(sorted(a)[:12]))
check("analytics is JSON-serialisable", bool(json.dumps(a, ensure_ascii=False)))
hm = fb.heatmap_series(1001)
check("heatmap starts on a Sunday", date.fromisoformat(hm[0]["date"]).weekday() == 6, hm[0]["date"])
check("unregistered user -> None", fb.build_personal_analytics(999) is None)

# ---- 5. website request goes ONLY to the asking socket -----------------
class WS:
    def __init__(self): self.out = []
    async def send(self, m): self.out.append(json.loads(m))
ws = WS()
asyncio.run(fb._handle_ws_request(ws, json.dumps({"type": "personal_analytics", "username": "sara"})))
check("PRIVACY: username lookup is refused", ws.out and ws.out[0].get("found") is False and ws.out[0].get("reason") == "use_link",
      str(ws.out[0]) if ws.out else "no reply")
tok = fb.get_or_create_analytics_token(1001)
check("token is stable for the same member", fb.get_or_create_analytics_token(1001) == tok)
check("tokens differ between members", fb.get_or_create_analytics_token(1002) != tok)
ws = WS()
asyncio.run(fb._handle_ws_request(ws, json.dumps({"type": "personal_analytics", "token": tok})))
check("private link opens own analytics", bool(ws.out) and ws.out[0].get("found") is True
      and ws.out[0]["data"]["name"] == "@sara", ws.out[0]["data"]["name"] if ws.out and ws.out[0].get("found") else str(ws.out))
for bad in ["", "x" * 10, tok[:-1] + ("A" if tok[-1] != "A" else "B"), "' OR 1=1 --", None, 12345]:
    ws = WS(); asyncio.run(fb._handle_ws_request(ws, json.dumps({"type": "personal_analytics", "token": bad})))
    if not (ws.out and ws.out[0].get("found") is False):
        check(f"bad token rejected: {bad!r}", False); break
else:
    check("wrong / malformed tokens all rejected", True)
new_tok = fb.regenerate_analytics_token(1001)
ws = WS(); asyncio.run(fb._handle_ws_request(ws, json.dumps({"type": "personal_analytics", "token": tok})))
check("/mylink جديد kills the old link", new_tok != tok and ws.out[0].get("found") is False)
check("link points at the site with #me=", fb.analytics_link_for(1001) == fb.SITE_URL + "#me=" + new_tok, fb.analytics_link_for(1001)[:45] + "…")

# /mylink command: private chat gives link; group never shows it
class Msg:
    def __init__(self): self.replies = []
    async def reply_text(self, text, **kw): self.replies.append((text, kw))
def upd(chat_type, uid=1001):
    m = Msg()
    return types.SimpleNamespace(effective_user=types.SimpleNamespace(id=uid, username="sara", full_name="S"),
                                 effective_chat=types.SimpleNamespace(type=chat_type), message=m), m
u, m = upd("supergroup"); asyncio.run(fb.cmd_mylink(u, ctx()))
check("/mylink in the group does NOT post the link", "#me=" not in json.dumps(m.replies, default=str, ensure_ascii=False), m.replies[0][0])
u, m = upd("private"); asyncio.run(fb.cmd_mylink(u, ctx()))
btn = m.replies[0][1]["reply_markup"].inline_keyboard[0][0]
check("/mylink in private sends a link button", btn.url == fb.analytics_link_for(1001), btn.text)
u, m = upd("private"); c_ = ctx(); c_.args = ["جديد"]; before = fb.analytics_link_for(1001)
asyncio.run(fb.cmd_mylink(u, c_))
check("/mylink جديد gives a different link", m.replies[0][1]["reply_markup"].inline_keyboard[0][0].url != before)
u, m = upd("private", uid=4242); asyncio.run(fb.cmd_mylink(u, ctx()))
check("/mylink for unregistered asks to register", "/register" in m.replies[0][0])
ws2 = WS(); asyncio.run(fb._handle_ws_request(ws2, "not json"))
check("garbage websocket message ignored", ws2.out == [])
exp = fb.build_export_data()
check("public export has no personal analytics", "heatmap" not in json.dumps(exp))

# ---- 6. /mycard image --------------------------------------------------
if fb.CARD_IMAGE_AVAILABLE:
    buf = fb.generate_stat_card_image(1001)
    data = buf.getvalue()
    open(os.path.join(tempfile.gettempdir(), "mycard_sample.png"), "wb").write(data)
    check("/mycard renders a PNG", data[:8] == b"\x89PNG\r\n\x1a\n", f"{len(data)//1024} KB")
else:
    check("/mycard renders a PNG", False, "raqm not available here")

# ---- 7. streak-save nudge (lina: 10-day streak, nothing today) ---------
c = ctx(); asyncio.run(fb.job_streak_save_nudge(c))
to_lina = [m for m in c.bot.sent if m[0] == 1003]
check("streak nudge DMs the at-risk member", len(to_lina) == 1, to_lina[0][1].splitlines()[0] if to_lina else "")
check("streak nudge skips people who logged today", not [m for m in c.bot.sent if m[0] == 1001])
c2 = ctx(); asyncio.run(fb.job_streak_save_nudge(c2))
check("streak nudge not sent twice same day", not c2.bot.sent)
check("streak nudge never posts to the group", all(m[0] != int(fb.GROUP_CHAT_ID) for m in c.bot.sent))

# ---- 8. monthly titles + monthly recap, pretending today is the 1st ----
first = today.replace(day=1)
real_today = fb.local_today
fb.local_today = lambda: first
c = ctx(); asyncio.run(fb.job_monthly_titles(c))
check("monthly titles posted", len(c.bot.sent) == 1, c.bot.sent[0][1].replace("\n", " | ") if c.bot.sent else "")
check("monthly titles saved in DB", bool(fb.get_monthly_titles()))
c = ctx(); asyncio.run(fb.job_monthly_recap(c))
txt = c.bot.sent[0][1] if c.bot.sent else ""
prev = (first - timedelta(days=1))
conn = fb.db()
expect = conn.execute(fb._EFFECTIVE_CTE + "SELECT SUM(minutes) m FROM daily_effective WHERE session_date < :until",
                      {"since": prev.replace(day=1).isoformat(), "until": first.isoformat()}).fetchone()["m"]
conn.close()
check("monthly recap covers the month that ended", str(expect) in txt and fb.ARABIC_MONTHS[prev.month - 1] in txt,
      txt.replace("\n", " | "))

# ---- 9. weekly personal recap counts only last week --------------------
sunday = fb.week_start_for(real_today())
fb.local_today = lambda: sunday
c = ctx(); asyncio.run(fb.job_weekly_personal_recap(c))
check("weekly personal recap DMs members", len(c.bot.sent) >= 2, f"{len(c.bot.sent)} DMs")
check("weekly recap DM has the private analytics button",
      all("reply_markup" in m[2] and "#me=" in m[2]["reply_markup"].inline_keyboard[0][0].url for m in c.bot.sent))
fb.local_today = real_today

# ---- 9b. daily poll is anonymous ---------------------------------------
c = ctx(); asyncio.run(fb.job_daily_poll(c))
check("daily group poll is anonymous", c.bot.sent and c.bot.sent[0][2].get("is_anonymous") is True)
u, m = upd("private", uid=1); c = ctx(); asyncio.run(fb.cmd_testpoll(u, c))  # uid 1 = manager
check("/testpoll preview is anonymous too", c.bot.sent and c.bot.sent[0][2].get("is_anonymous") is True)

# ---- 10. things that were already on main still work -------------------
check("display_streak still present", fb.display_streak(1003) >= 10, str(fb.display_streak(1003)))
check("time-window dedup still present", fb.extract_time_window("14:08 - 15:08") == "14:08-15:08")
check("960-min daily cap still present", fb.MAX_DAILY_CARD_MINUTES == 960)

# ---- 11. bot wiring: every handler + job registers ----------------------
from telegram.ext import Application
captured = {}
def fake_run_polling(self, *a, **k): captured["app"] = self
Application.run_polling = fake_run_polling
fb.main()
app = captured["app"]
cmds = {c for h in app.handlers[0] if hasattr(h, "commands") for c in h.commands}
for c_ in ["mycard", "mylink", "findpartner", "me", "stats", "testschedule", "pausescheduleposts"]:
    check(f"/{c_} registered", c_ in cmds)
jobs = {j.callback.__name__: j for j in app.job_queue.jobs()}
for j in ["job_monthly_titles", "job_streak_save_nudge", "job_weekly_personal_recap", "job_auto_complete_sessions"]:
    check(f"{j} scheduled", j in jobs)
def first_local_run(name):
    from datetime import timezone
    trig = jobs[name].job.trigger
    nxt = trig.get_next_fire_time(None, datetime.now(timezone.utc))
    return nxt.astimezone(__import__("datetime").timezone(timedelta(hours=3)))
wl = first_local_run("job_weekly_leaderboard"); wr = first_local_run("job_weekly_personal_recap")
check("weekly leaderboard fires on SATURDAY 23:30 Jeddah", wl.strftime("%A %H:%M") == "Saturday 23:30", wl.strftime("%A %H:%M"))
check("weekly personal recap fires on SUNDAY 10:00 Jeddah", wr.strftime("%A %H:%M") == "Sunday 10:00", wr.strftime("%A %H:%M"))

print(f"\n{sum(results)}/{len(results)} passed")
shutil.rmtree(tmp)
sys.exit(0 if all(results) else 1)
