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

# ---- 9c. private DB backup + /backupnow (GitHub faked, nothing uploaded) --
import base64 as _b64
class FakeResp:
    def __init__(self, code, body=None): self.status_code, self._b, self.text = code, body or {}, json.dumps(body or {})
    def json(self): return self._b
uploads = []
def fake_github(get_code=404, put_codes=(201,), private=True):
    codes = list(put_codes)
    def get(url, **kw):
        if "/contents/" not in url:
            return FakeResp(200, {"private": private})
        return FakeResp(get_code, {"sha": "abc"} if get_code == 200 else {})
    fb.requests.get = get
    def put(url, json=None, **kw):
        uploads.append((url, json)); return FakeResp(codes.pop(0) if codes else 201)
    fb.requests.put = put
fb.GITHUB_TOKEN = "fake"; fb.BACKUP_GITHUB_REPO = ""
check("backup refuses without a private repo set", fb.backup_database_to_github() is False and "BACKUP_GITHUB_REPO" in fb._LAST_GITHUB_ERROR)
fb.BACKUP_GITHUB_REPO = "kauforest/kau-forest-backups"
uploads.clear(); fake_github(get_code=404, put_codes=(422, 201))
ok = fb.backup_database_to_github()
check("first backup into a brand-new EMPTY repo works", ok and len(uploads) == 2 and "branch" not in uploads[-1][1])
check("backup goes to the private repo, never the public one", all("kau-forest-backups" in u[0] for u in uploads), uploads[-1][0])
snap = os.path.join(tmp, "restored.db"); open(snap, "wb").write(_b64.b64decode(uploads[-1][1]["content"]))
n_backup = sqlite3.connect(snap).execute("select count(*) from sessions").fetchone()[0]
n_live = fb.db().execute("select count(*) from sessions").fetchone()[0]
check("backup file is a valid DB with every session", n_backup == n_live, f"{n_backup} sessions")
uploads.clear(); fake_github(get_code=200, put_codes=(201,))
check("daily backup updates the same file", fb.backup_database_to_github() and uploads[0][1].get("sha") == "abc")
uploads.clear(); fake_github(private=False)
check("REFUSES to back up into a PUBLIC repo", not fb.backup_database_to_github() and not uploads and "PUBLIC" in fb._LAST_GITHUB_ERROR, fb._LAST_GITHUB_ERROR)
fb.GITHUB_REPO = "kauforest/kau-forest-backups"; uploads.clear(); fake_github()
check("REFUSES if backup repo = website repo", not fb.backup_database_to_github() and not uploads)
fb.GITHUB_REPO = "kauforest/kau-forest-bot"
fake_github(get_code=200, put_codes=(401,))
check("expired token -> clear reason", not fb.backup_database_to_github() and "expired" in fb._LAST_GITHUB_ERROR, fb._LAST_GITHUB_ERROR)
fake_github(get_code=200, put_codes=(403,))
check("no write permission -> clear reason", not fb.backup_database_to_github() and "Read and write" in fb._LAST_GITHUB_ERROR, fb._LAST_GITHUB_ERROR)
u, m = upd("private", uid=1001); asyncio.run(fb.cmd_backupnow(u, ctx()))
check("/backupnow is managers-only", "للمنظّمين" in m.replies[0][0])
u, m = upd("supergroup", uid=1); asyncio.run(fb.cmd_backupnow(u, ctx()))
check("/backupnow refuses in the group", "بالخاص" in m.replies[0][0])
fake_github(get_code=404, put_codes=(201,))
u, m = upd("private", uid=1); asyncio.run(fb.cmd_backupnow(u, ctx()))
check("/backupnow success message", "✅" in m.replies[-1][0], m.replies[-1][0].splitlines()[0])
fake_github(get_code=200, put_codes=(401,))
u, m = upd("private", uid=1); asyncio.run(fb.cmd_backupnow(u, ctx()))
check("/backupnow failure explains why", "❌" in m.replies[-1][0] and "401" in m.replies[-1][0], m.replies[-1][0].replace("\n", " | "))
fb.BACKUP_GITHUB_REPO = ""; fb.GITHUB_TOKEN = ""

# ---- 9d. Forest auto-host (fake PC helper, fake Telegram) ---------------
import asyncio as _aio
class Sent:
    def __init__(self): self.msgs = []; self.edits = []; self.n = 100
    async def send_message(self, chat_id, text, **kw):
        self.n += 1; self.msgs.append((chat_id, text, kw)); return types.SimpleNamespace(message_id=self.n)
    async def edit_message_text(self, chat_id, message_id, text, **kw): self.edits.append((message_id, text))
class App:
    def __init__(self): self.bot_data = {}; self.tasks = []
    def create_task(self, coro): self.tasks.append(_aio.ensure_future(coro))
class FakeAgent:
    """Answers bot commands like host_agent.py would."""
    def __init__(self, start_results): self.cmds = []; self.start_results = list(start_results)
    async def send(self, raw):
        m = json.loads(raw)
        if m.get("type") != "cmd":
            return
        self.cmds.append(m["action"])
        if m["action"] == "create_room": res = {"ok": True, "code": "ABCD123G"}
        elif m["action"] == "start_room": res = self.start_results.pop(0)
        else: res = {"ok": True, "closed": True}
        _aio.get_running_loop().call_soon(lambda: _aio.ensure_future(
            fb._handle_agent_message(self, {"type": "cmd_result", "id": m["id"], **res})))
    async def close(self): pass

async def auto_host_tests():
    fb.GROUP_CHAT_ID = "-100123"; fb.TOPIC_SESSIONS_ID = "55"
    fb.set_setting("schedule_posting_enabled", "true")
    real_now = fb.local_now

    def make_ctx():
        c = types.SimpleNamespace(bot=Sent(), application=App(), args=[])
        return c
    async def settle(c):
        for _ in range(5):
            await _aio.sleep(0)
            if c.application.tasks:
                await _aio.gather(*c.application.tasks); c.application.tasks.clear()

    # secret handling
    fb.HOST_AGENT_SECRET = ""
    ag = FakeAgent([])
    await fb._handle_agent_message(ag, {"type": "agent_hello", "secret": "x"})
    check("auto-host: no secret set -> helper refused", not fb.agent_connected())
    fb.HOST_AGENT_SECRET = "s3cret-value-xyz"
    await fb._handle_agent_message(ag, {"type": "agent_hello", "secret": "wrong"})
    check("auto-host: wrong secret -> refused", not fb.agent_connected())

    def plan_day(c, minutes_until_start):
        base = real_now().replace(second=0, microsecond=0)
        st = base + timedelta(minutes=minutes_until_start)
        sched = {"date": fb.local_today().isoformat(), "blocks": [(st, st + timedelta(minutes=60))],
                 "gap_labels": {}, "pinged": set(), "done": set(), "link_sent": set(), "rooms": {}}
        c.application.bot_data["today_schedule"] = sched; fb._save_schedule_state(sched)
        return base, st

    # --- scenario A: refused at start, starts on the 1-minute retry
    c = make_ctx(); base, st = plan_day(c, 7)
    agA = FakeAgent([{"ok": True, "started": False, "members": 0}, {"ok": True, "started": True, "members": 2}])
    await fb._handle_agent_message(agA, {"type": "agent_hello", "secret": "s3cret-value-xyz"})
    check("auto-host: correct secret -> helper connected", fb.agent_connected())
    fb.set_setting("auto_host_enabled", "true")
    fb.local_now = lambda: base
    await fb.job_check_schedule_pings(c); await settle(c)
    post = [m for m in c.bot.msgs if "forestapp.cc/join-room?token=ABCD123G" in m[1]]
    check("auto-host: room link posted 8 min before (7 min left)", len(post) == 1 and post[0][2].get("message_thread_id") == 55,
          post[0][1].splitlines()[0] if post else str(c.bot.msgs))
    check("auto-host: old 'an admin shares a link' ping NOT sent", not any("أحد الأدمنز" in m[1] for m in c.bot.msgs))
    check("auto-host: code shown with LRM, no early start", "\u200eABCD123G" in post[0][1] and "start_room" not in agA.cmds)
    fb.local_now = lambda: st - timedelta(minutes=3)
    await fb.job_check_schedule_pings(c); await settle(c)
    check("auto-host: still waits before start time", agA.cmds.count("start_room") == 0 and agA.cmds.count("create_room") == 1)
    fb.local_now = lambda: st
    await fb.job_check_schedule_pings(c); await settle(c)
    room = fb._load_schedule_state()["rooms"][st.isoformat()]
    check("auto-host: <2 people at start -> waits 1 minute", room["state"] == "waiting_retry" and agA.cmds.count("start_room") == 1, room["state"])
    fb.local_now = lambda: st + timedelta(seconds=30)
    await fb.job_check_schedule_pings(c); await settle(c)
    check("auto-host: no retry before the minute is up", agA.cmds.count("start_room") == 1)
    check("auto-host: retry is due exactly 1 min after the block start",
          fb._load_schedule_state()["rooms"][st.isoformat()]["next_try"] == (st + timedelta(seconds=60)).isoformat())
    fb.local_now = lambda: st + timedelta(seconds=62)  # the next once-a-minute tick
    await fb.job_check_schedule_pings(c); await settle(c)
    room = fb._load_schedule_state()["rooms"][st.isoformat()]
    check("auto-host: second try starts the session", room["state"] == "started" and agA.cmds.count("start_room") == 2, room["state"])
    check("auto-host: post edited to 'started'", any("بدأت الجلسة" in e[1] for e in c.bot.edits))
    check("auto-host: block will auto-tick ✅", st.isoformat() in fb._load_schedule_state()["link_sent"])

    # --- scenario A2: prepared at 10 min, link posted exactly at 8 min
    c = make_ctx(); base, st = plan_day(c, 10)
    agP = FakeAgent([])
    await fb._handle_agent_message(agP, {"type": "agent_hello", "secret": "s3cret-value-xyz"})
    fb.local_now = lambda: base
    await fb.job_check_schedule_pings(c); await settle(c)
    room = fb._load_schedule_state()["rooms"][st.isoformat()]
    check("auto-host: room PREPARED 10 min before, not posted yet", agP.cmds == ["create_room"] and room["state"] == "ready"
          and not c.bot.msgs, room["state"])
    fb.local_now = lambda: st - timedelta(minutes=9)
    await fb.job_check_schedule_pings(c); await settle(c)
    check("auto-host: still not posted at 9 min", not c.bot.msgs and agP.cmds.count("create_room") == 1)
    fb.local_now = lambda: st - timedelta(minutes=8)
    await fb.job_check_schedule_pings(c); await settle(c)
    room = fb._load_schedule_state()["rooms"][st.isoformat()]
    check("auto-host: link posted exactly at 8 min", len(c.bot.msgs) == 1 and room["state"] == "open"
          and "ABCD123G" in c.bot.msgs[0][1], room["state"])
    fb.local_now = lambda: st - timedelta(minutes=7)
    await fb.job_check_schedule_pings(c); await settle(c)
    check("auto-host: posted only once", len(c.bot.msgs) == 1)

    # --- scenario A3: previous session still running at prep time -> retry next minute (live bug 2026-11-16 10:30)
    class BusyThenOK(FakeAgent):
        def __init__(self): super().__init__([]); self.busy = 1
        async def send(self, raw):
            m = json.loads(raw)
            if m.get("type") == "cmd" and m["action"] == "create_room" and self.busy:
                self.busy -= 1; self.cmds.append("create_room")
                _aio.get_running_loop().call_soon(lambda: _aio.ensure_future(fb._handle_agent_message(
                    self, {"type": "cmd_result", "id": m["id"], "ok": False, "error": "a session is still running"})))
                return
            await super().send(raw)
    c = make_ctx(); base, st = plan_day(c, 10)
    agBusy = BusyThenOK()
    await fb._handle_agent_message(agBusy, {"type": "agent_hello", "secret": "s3cret-value-xyz"})
    fb.local_now = lambda: base
    await fb.job_check_schedule_pings(c); await settle(c)
    room = fb._load_schedule_state()["rooms"][st.isoformat()]
    check("auto-host: PC busy at prep time -> will retry (no alarm, no old ping)", room["state"] == "create_retry"
          and not c.bot.msgs, room["state"])
    fb.local_now = lambda: base + timedelta(minutes=1)
    await fb.job_check_schedule_pings(c); await settle(c)
    fb.local_now = lambda: st - timedelta(minutes=8)
    await fb.job_check_schedule_pings(c); await settle(c)
    room = fb._load_schedule_state()["rooms"][st.isoformat()]
    check("auto-host: retry a minute later succeeds, link still posted at 8 min", agBusy.cmds.count("create_room") == 2
          and room["state"] == "open" and len(c.bot.msgs) == 1 and "ABCD123G" in c.bot.msgs[0][1], room["state"])
    # busy all the way -> gives up 3 min before and the old ping covers it
    class AlwaysBusy(BusyThenOK):
        def __init__(self): super().__init__(); self.busy = 99
    c = make_ctx(); base, st = plan_day(c, 10)
    agAB = AlwaysBusy()
    await fb._handle_agent_message(agAB, {"type": "agent_hello", "secret": "s3cret-value-xyz"})
    for m_before in (10, 9, 8, 7, 6, 5, 4, 3):
        fb.local_now = lambda m_before=m_before: st - timedelta(minutes=m_before)
        await fb.job_check_schedule_pings(c); await settle(c)
    room = fb._load_schedule_state()["rooms"][st.isoformat()]
    check("auto-host: still busy at 3 min -> gives up, old ping as backup", room["state"] == "create_failed"
          and any("أحد الأدمنز" in m[1] for m in c.bot.msgs), room["state"])

    # --- scenario B: nobody joins -> calm message + room closed
    c = make_ctx(); base, st = plan_day(c, 6)
    agB = FakeAgent([{"ok": True, "started": False, "members": 0}, {"ok": True, "started": False, "members": 0}])
    await fb._handle_agent_message(agB, {"type": "agent_hello", "secret": "s3cret-value-xyz"})
    for t in (base, st, st + timedelta(seconds=61)):
        fb.local_now = lambda t=t: t
        await fb.job_check_schedule_pings(c); await settle(c)
    room = fb._load_schedule_state()["rooms"][st.isoformat()]
    check("auto-host: still <2 after retry -> 'not enough' + close", room["state"] == "not_enough" and "close_room" in agB.cmds
          and any("ما اكتمل العدد" in e[1] for e in c.bot.edits), room["state"])

    # --- scenario C: helper offline -> old manual-host ping as before
    fb._agent_disconnected(agB)
    c = make_ctx(); base, st = plan_day(c, 4)
    fb.local_now = lambda: base
    await fb.job_check_schedule_pings(c); await settle(c)
    check("auto-host: helper offline -> falls back to the old ping", any("أحد الأدمنز" in m[1] for m in c.bot.msgs))

    # --- scenario D: auto-host switched off -> old ping
    agD = FakeAgent([])
    await fb._handle_agent_message(agD, {"type": "agent_hello", "secret": "s3cret-value-xyz"})
    fb.set_setting("auto_host_enabled", "false")
    c = make_ctx(); base, st = plan_day(c, 4)
    fb.local_now = lambda: base
    await fb.job_check_schedule_pings(c); await settle(c)
    check("auto-host: switched off -> no room, old ping", "create_room" not in agD.cmds and any("أحد الأدمنز" in m[1] for m in c.bot.msgs))

    # --- scenario E: group posting kill-switch off -> nothing at all
    fb.set_setting("auto_host_enabled", "true"); fb.set_setting("schedule_posting_enabled", "false")
    c = make_ctx(); base, st = plan_day(c, 7)
    fb.local_now = lambda: base
    await fb.job_check_schedule_pings(c); await settle(c)
    check("auto-host: schedule posting paused -> nothing posted", not c.bot.msgs and "create_room" not in agD.cmds)
    fb.set_setting("schedule_posting_enabled", "true")

    # --- /autohost command
    um = Msg()
    u = types.SimpleNamespace(effective_user=types.SimpleNamespace(id=1), effective_chat=types.SimpleNamespace(type="private"), message=um)
    await fb.cmd_autohost(u, types.SimpleNamespace(args=["off"]))
    check("/autohost off works + shows helper online", fb.get_setting("auto_host_enabled") == "false" and "🟢" in um.replies[-1][0])
    u2 = types.SimpleNamespace(effective_user=types.SimpleNamespace(id=1001), effective_chat=types.SimpleNamespace(type="private"), message=Msg())
    await fb.cmd_autohost(u2, types.SimpleNamespace(args=["on"]))
    check("/autohost is managers-only", fb.get_setting("auto_host_enabled") == "false")
    fb._agent_disconnected(agD)
    fb.local_now = real_now; fb.GROUP_CHAT_ID = "-100123"

_aio.run(auto_host_tests())

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
for c_ in ["mycard", "mylink", "backupnow", "autohost", "testroom", "findpartner", "me", "stats", "testschedule", "pausescheduleposts"]:
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
