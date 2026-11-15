"""End-to-end: real bot WebSocket code + real docs/index.html in headless Chrome.
Local only (temp DB, fake members, page pointed at localhost). Needs playwright + Chrome.
Run:  python3 tests/test_website_e2e.py"""
import asyncio, os, sys, tempfile, json, threading, http.server, functools, random
from datetime import timedelta, datetime

REPO = sys.argv[1] if len(sys.argv) > 1 else os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
tmp = tempfile.mkdtemp()
os.environ.update(DB_PATH=os.path.join(tmp, "t.db"), BOT_TOKEN="x", TZ_OFFSET_HOURS="3")
sys.path.insert(0, REPO)
import forest_bot as fb
import websockets

fb.init_db()
fb.register_user(1001, "@sara", "Med25")
fb.register_user(1002, "@omar", "Med26")
today = fb.local_today()
conn = fb.db()
for d in range(40):
    day = today - timedelta(days=d)
    for uid in (1001, 1002):
        conn.execute("INSERT INTO sessions (user_id, username, minutes, tag, logged_at, session_date, source) VALUES (?,?,?,?,?,?,?)",
                     (uid, "sara" if uid == 1001 else "omar", random.randint(30, 200), None,
                      datetime.combine(day, datetime.min.time()).replace(hour=14).isoformat(), day.isoformat(), "session"))
conn.commit(); conn.close()
sara_link_token = fb.get_or_create_analytics_token(1001)

# page copy pointed at local websocket
site = os.path.join(tmp, "site"); os.makedirs(site)
html = open(os.path.join(REPO, "docs/index.html")).read().replace(
    "wss://kau-forest-bot-production.up.railway.app", "ws://127.0.0.1:8765")
open(os.path.join(site, "index.html"), "w").write(html)
for f in ("data.json",):
    src = os.path.join(REPO, "docs", f)
    if os.path.exists(src):
        open(os.path.join(site, f), "w").write(open(src).read())
handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=site)
handler.log_message = lambda *a: None
httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 8766), handler)
threading.Thread(target=httpd.serve_forever, daemon=True).start()

def run_ws():
    async def main():
        async with websockets.serve(fb.ws_handler, "127.0.0.1", 8765):
            await asyncio.Future()
    asyncio.run(main())
threading.Thread(target=run_ws, daemon=True).start()

from playwright.sync_api import sync_playwright
ok = []
def check(name, cond, detail=""):
    ok.append(bool(cond)); print(("PASS " if cond else "FAIL ") + name + (f"  — {detail}" if detail else ""))

with sync_playwright() as p:
    b = p.chromium.launch(executable_path="/usr/bin/google-chrome", args=["--no-sandbox"])
    pg = b.new_page(viewport={"width": 430, "height": 900})
    base = "http://127.0.0.1:8766/index.html"

    # 1. no link: the section shows instructions, no username box
    pg.goto(base); pg.wait_for_timeout(1500)
    pg.click("#maToggleBtn"); pg.wait_for_timeout(500)
    check("no username box any more", pg.locator("input#maUsernameInput").count() == 0)
    check("shows the /mylink instructions", pg.locator("#maHowto").is_visible(), pg.inner_text("#maHowto").replace("\n", " "))

    # 2. open via private link
    pg.goto(f"{base}#me={sara_link_token}"); pg.wait_for_timeout(2500)
    txt = pg.inner_text("#maResult")
    check("private link shows Sara's analytics", "@sara" in txt or "sara" in txt, txt[:60].replace("\n", " "))
    check("token wiped from the address bar", "#me=" not in pg.url, pg.url)
    pg.locator("#myAnalyticsSection").screenshot(path=os.path.join(tmp, "site_private_link.png"))

    # 3. comes back later without the link: remembered on this device
    pg.goto(base); pg.wait_for_timeout(1500); pg.click("#maToggleBtn"); pg.wait_for_timeout(1500)
    check("remembered on the same device", "sara" in pg.inner_text("#maResult"))

    # 4. forget button
    pg.click("#maForgetBtn"); pg.wait_for_timeout(300)
    check("'hide from this device' clears it", "sara" not in pg.inner_text("#maResult") and pg.locator("#maHowto").is_visible())

    # 5. a different person's browser can't see Sara
    pg2 = b.new_context().new_page()
    pg2.goto(base); pg2.wait_for_timeout(1500); pg2.click("#maToggleBtn"); pg2.wait_for_timeout(800)
    check("another browser sees nothing", "sara" not in pg2.inner_text("#maResult"))

    # 6. old/renewed link
    fb.regenerate_analytics_token(1001)
    pg2.goto(f"{base}#me={sara_link_token}"); pg2.wait_for_timeout(2500)
    t2 = pg2.inner_text("#maResult")
    check("old link after /mylink جديد shows 'get a new link'", "/mylink" in t2 and "sara" not in t2, t2.replace("\n", " ")[:70])
    pg2.locator("#myAnalyticsSection").screenshot(path=os.path.join(tmp, "site_old_link.png"))

    # 7. leaderboard still loads live
    check("leaderboard still loads", pg.locator("text=@sara").count() + pg.locator("text=@omar").count() > 0)
    b.close()

print(f"\n{sum(ok)}/{len(ok)} passed")
httpd.shutdown()
