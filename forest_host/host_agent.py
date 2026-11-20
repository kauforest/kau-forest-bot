"""غابة الطب — Forest auto-host helper (runs on Husain's PC, next to BlueStacks 5).

What it does:
  * Connects OUT to the bot (same WebSocket the website uses), proves itself
    with a secret, then waits for commands from the bot.
  * The bot decides WHEN (it knows the study schedule). This program only does
    HOW, by tapping the Forest app inside BlueStacks through ADB:
      create_room(minutes) -> sets the timer, Plant Together -> Create Room,
                              reads the room code and sends it back
      start_room()         -> presses Start (only on the room screen); if Forest
                              says "at least 2 people" it taps OK and reports it
      close_room()         -> Exit Room -> Exit
  * After a session ends it taps Continue -> All Done so it's ready again.

  * Self-healing (v3): every few minutes it checks that BlueStacks answers and
    has internet. If BlueStacks froze or lost its network, it restarts it on
    its own (never during a running session) and reopens Forest.

Run:   python host_agent.py
Test:  python host_agent.py --selftest     (opens a 10-min room, prints the code, closes it)
       python host_agent.py --screen       (prints what screen Forest is on)
       python host_agent.py --health       (checks BlueStacks + internet, changes nothing)
       python host_agent.py --heal         (restarts BlueStacks now and waits until it's ready)
       python host_agent.py --install-startup  (start this helper automatically when Windows logs in)

Needs: BlueStacks 5 open with ADB on, and `pip install websockets`.
"""
import asyncio
import json
import logging
import os
import re
import secrets
import subprocess
import sys
import threading
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta

BOT_WS_URL = os.environ.get("FOREST_BOT_WS", "wss://kau-forest-bot-production.up.railway.app")
VERSION = 6

ADB_CANDIDATES = [
    (r"C:\Program Files\BlueStacks_nxt\HD-Adb.exe", "127.0.0.1:5555"),
    (r"C:\Program Files\BlueStacks\HD-Adb.exe", "127.0.0.1:5555"),
]
ADB, DEVICE = next(((p, d) for p, d in ADB_CANDIDATES if os.path.exists(p)), ADB_CANDIDATES[0])
PKG = "cc.forestapp"
BS_DIR = os.path.dirname(ADB)
HD_PLAYER = os.path.join(BS_DIR, "HD-Player.exe")
BS_CONF_CANDIDATES = [r"C:\ProgramData\BlueStacks_nxt\bluestacks.conf", r"C:\ProgramData\BlueStacks\bluestacks.conf"]

HEAL_CHECK_EVERY = 300        # watchdog looks at BlueStacks every 5 min
HEAL_MIN_GAP = 10 * 60        # never restart BlueStacks more often than this…
HEAL_NET_MIN_GAP = 2 * 3600   # …and for "no internet" at most every 2 h (a restart may not fix e.g. a firewall)
BOOT_WAIT = 240               # how long to wait for Android to finish booting after a restart

HERE = os.path.dirname(os.path.abspath(__file__))
SECRET_FILE = os.path.join(HERE, "secret.txt")
STATE_FILE = os.path.join(HERE, "agent_state.json")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.FileHandler(os.path.join(HERE, "agent.log"), encoding="utf-8"), logging.StreamHandler()],
)
log = logging.getLogger("host")

# Fixed spots on BlueStacks 5 (1080x1920 portrait). Everything that Forest
# exposes as text is found by its text instead; these are only for the parts
# of Forest's home screen that have no readable labels.
TIMER = (540, 1650)
ROOM_START = (540, 1770)
ROOM_EXIT = (540, 1869)
ROOM_AVATARS = (528, 489)
END_CONTINUE = (540, 1839)
RULER_Y = 1446


class ForestError(Exception):
    pass


# ---------------------------------------------------------------- state file
def load_state() -> dict:
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_state(**changes):
    st = load_state()
    st.update(changes)
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(st, f)


def session_running() -> bool:
    until = load_state().get("session_until")
    return bool(until) and datetime.now() < datetime.fromisoformat(until)


# ---------------------------------------------------------------- ADB layer
def adb(*args, timeout=40, binary=False):
    try:
        r = subprocess.run([ADB, "-s", DEVICE, *args], capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise ForestError(f"adb timeout: {' '.join(args)[:60]}")
    return r.stdout if binary else (r.stdout + r.stderr).decode("utf-8", "replace")


def _adb_connect_once() -> str:
    try:
        return subprocess.run([ADB, "connect", DEVICE], capture_output=True, text=True, timeout=20).stdout
    except subprocess.TimeoutExpired:
        return "timeout"


def adb_connect():
    if HEALER.busy:
        raise ForestError("BlueStacks is restarting right now — try again in a minute")
    out = _adb_connect_once()
    if "connected" not in out:
        subprocess.run([ADB, "disconnect", DEVICE], capture_output=True, timeout=20)
        time.sleep(2)
        out = _adb_connect_once()
    if "connected" not in out:
        HEALER.request("ADB can't reach BlueStacks")
        raise ForestError(f"can't reach BlueStacks ({out.strip()[:80]}) — restarting BlueStacks")


def android_ready() -> bool:
    """Android inside BlueStacks has finished booting and answers commands."""
    try:
        return adb("shell", "getprop", "sys.boot_completed", timeout=15).strip().endswith("1")
    except ForestError:
        return False


PING_SEEN_OK = False  # some emulator networks drop ping entirely; only trust a "no" once a "yes" was seen


def has_internet() -> bool | None:
    """True/False if Android can reach the internet; None if we can't tell."""
    global PING_SEEN_OK
    try:
        out = adb("shell", "ping -c 1 -W 4 1.1.1.1 || ping -c 1 -W 4 8.8.8.8", timeout=20)
    except ForestError:
        return None
    if " 0% packet loss" in out or "1 received" in out:
        PING_SEEN_OK = True
        return True
    if "100% packet loss" in out or "unreachable" in out.lower() or "0 received" in out:
        return False if PING_SEEN_OK else None
    return None


def room_recently_opened(minutes=20) -> bool:
    t = load_state().get("room_created_at")
    return bool(t) and datetime.now() - datetime.fromisoformat(t) < timedelta(minutes=minutes)


def tap(x, y, pause=1.5):
    adb("shell", "input", "tap", str(int(x)), str(int(y)))
    time.sleep(pause)


def swipe(x1, x2, y, ms):
    adb("shell", "input", "swipe", str(int(x1)), str(int(y)), str(int(x2)), str(int(y)), str(int(ms)))
    time.sleep(1.2)


def back(pause=1.5):
    adb("shell", "input", "keyevent", "KEYCODE_BACK")
    time.sleep(pause)


def dump() -> list[dict]:
    for _ in range(3):
        out = adb("shell", "uiautomator", "dump", "/sdcard/host_ui.xml", timeout=60)
        if "dumped" in out.lower():
            xml = adb("shell", "cat", "/sdcard/host_ui.xml")
            try:
                root = ET.fromstring(xml[xml.find("<?xml"):] if "<?xml" in xml else xml)
            except ET.ParseError:
                time.sleep(1)
                continue
            nodes = []
            for n in root.iter("node"):
                b = re.findall(r"\d+", n.attrib.get("bounds", ""))
                nodes.append({
                    "text": n.attrib.get("text", ""),
                    "desc": n.attrib.get("content-desc", ""),
                    "click": n.attrib.get("clickable") == "true",
                    "b": tuple(map(int, b)) if len(b) == 4 else (0, 0, 0, 0),
                })
            return nodes
        time.sleep(1.5)
    raise ForestError("couldn't read the Forest screen (ui dump failed)")


def find(nodes, text=None, desc=None, contains=None):
    for n in nodes:
        if text is not None and n["text"] == text:
            return n
        if desc is not None and n["desc"] == desc:
            return n
        if contains is not None and contains in n["text"]:
            return n
    return None


def center(node):
    x1, y1, x2, y2 = node["b"]
    return (x1 + x2) // 2, (y1 + y2) // 2


def screen_pixels():
    raw = adb("exec-out", "screencap", binary=True, timeout=30)
    if len(raw) < 16:
        raise ForestError("screenshot failed")
    w = int.from_bytes(raw[0:4], "little")
    h = int.from_bytes(raw[4:8], "little")
    header = len(raw) - w * h * 4
    if w <= 0 or h <= 0 or header not in (12, 16):
        raise ForestError(f"unexpected screenshot format ({w}x{h}, header {header})")
    return w, h, header, raw


def _px(img, x, y):
    w, h, header, raw = img
    i = header + (y * w + x) * 4
    return raw[i], raw[i + 1], raw[i + 2]


def _bright_ratio(img, x1, y1, x2, y2):
    total = hit = 0
    for x in range(x1, x2, 4):
        for y in range(y1, y2, 3):
            r, g, b = _px(img, x, y)
            total += 1
            hit += r > 200 and g > 200 and b > 200
    return hit / max(total, 1)


def _vivid_ratio(img, x1, y1, x2, y2):
    """Share of bright, coloured (not white) pixels — e.g. Forest Plus's
    green→cyan "TRY 1 WEEK FOR FREE" button."""
    total = hit = 0
    for x in range(x1, x2, 4):
        for y in range(y1, y2, 3):
            r, g, b = _px(img, x, y)
            total += 1
            hit += g > 205 and min(r, b) < 170
    return hit / max(total, 1)


PROMO_CLOSE = (1040, 90)  # the grey ⓧ top-right of the Forest Plus ad (measured 2026-11-18)

# ---------------------------------------------------------------- screen logic
def forest_in_front() -> bool:
    out = adb("shell", "dumpsys window | grep mCurrentFocus")
    return PKG in out


def open_forest():
    if not forest_in_front():
        adb("shell", "monkey", "-p", PKG, "-c", "android.intent.category.LAUNCHER", "1")
        time.sleep(6)
    if not forest_in_front():
        raise ForestError("Forest didn't come to the front")


def classify() -> str:
    """What Forest is showing right now. Text first (dialogs/sheets), then
    colours for Forest's unlabelled home screens (measured on BlueStacks 5)."""
    nodes = dump()
    texts = " | ".join(n["text"] for n in nodes)
    if "All Done" in texts:
        return "all_done"
    if "No Internet Connection" in texts:
        return "no_internet_dialog"
    if "Something went wrong" in texts:
        return "error_dialog"
    if "at least 2 people" in texts:
        return "need_two_dialog"
    if "Exit the room?" in texts:
        return "exit_dialog"
    if "Room code" in texts:
        return "room_sheet"
    if find(nodes, desc="Close sheet"):
        return "sheet"
    img = screen_pixels()
    if img[0] != 1080 or img[1] != 1920:
        return f"unexpected_size_{img[0]}x{img[1]}"
    bg_g = _px(img, 100, 1300)[1]
    exit_txt = _bright_ratio(img, 470, 1855, 610, 1885)
    cont_bar = _bright_ratio(img, 150, 1825, 930, 1855)
    room_bar = _bright_ratio(img, 150, 1755, 930, 1785)
    if _vivid_ratio(img, 150, 1790, 930, 1840) > 0.7 and _vivid_ratio(img, 150, 1870, 930, 1910) < 0.2:
        # Forest Plus ads (dark "Grow Your Inner Forest" 2026-11-18, teal "Keep Apps that Help
        # You Focus Deeper" 2026-11-19): any background, but always the same wide
        # green→cyan "TRY 1 WEEK FOR FREE" button as a band at y≈1780–1850.
        return "promo"
    if bg_g < 70 and cont_bar > 0.8:
        return "end_screen"
    if bg_g > 140 and exit_txt > 0.05 and room_bar > 0.8:
        return "room_home"
    if bg_g > 140 and exit_txt < 0.02 and cont_bar > 0.4:
        return "home"
    if room_bar < 0.1 and cont_bar < 0.1:
        return "running"
    return "unknown"


def tidy(max_steps=6) -> str:
    """Clears leftover dialogs/sheets/end screens until Forest is on a home
    screen (with or without a room). Never touches a running session."""
    for _ in range(max_steps):
        state = classify()
        log.info("screen: %s", state)
        if state in ("home", "room_home", "running"):
            return state
        nodes = dump()
        if state == "all_done":
            tap(*center(find(nodes, text="All Done")))
        elif state in ("need_two_dialog", "no_internet_dialog", "error_dialog"):
            ok = find(nodes, text="OK")
            if ok:
                tap(*center(ok))
            else:
                back()
        elif state == "exit_dialog":
            tap(*center(find(nodes, text="Cancel")))
        elif state == "end_screen":
            tap(*END_CONTINUE, pause=3)
        elif state == "promo":
            # Close the ad with its ⓧ (never the trial button); Back if that didn't do it.
            close = find(nodes, desc="Close") or find(nodes, text="Close")
            tap(*(center(close) if close else PROMO_CLOSE), pause=2)
            if classify() == "promo":
                back()
        elif state in ("sheet", "room_sheet"):
            back()
        else:
            time.sleep(2)  # unknown: wait and look again — never press Back blindly (could end a session)
    return classify()


def settled_screen(looks=5, gap=3) -> str:
    """tidy(), but a "running" reading is only believed if it stays.
    When WE haven't started a session, a dark bottom bar is usually a passing
    overlay (someone joining, a toast, an animation) — on 2026-11-17 19:56 one
    such moment made the helper give up on a room 3 s before it read fine."""
    state = tidy()
    while state == "running" and not session_running() and looks > 1:
        looks -= 1
        time.sleep(gap)
        state = tidy()
        log.info("re-look: %s", state)
    return state


def read_minutes(nodes) -> int | None:
    if find(nodes, text="Stopwatch"):
        return 0
    n = find(nodes, contains=" minute")
    if n:
        m = re.match(r"(\d+) minute", n["text"])
        return int(m.group(1)) if m else None
    return None


def ruler_y(nodes) -> int:
    """The minute ruler sits ~73 px under the "N minute" label. Measured from
    the label because the sheet moves: on 2026-11-19 Forest added a "Deep focus /
    Customize allowed apps" block and the ruler went from y 1446 to ~1374."""
    label = find(nodes, contains=" minute") or find(nodes, text="Stopwatch")
    if not label:
        return RULER_Y
    y1, y2 = label["b"][1], label["b"][3]
    return (y1 + y2) // 2 + 73


def set_timer(target: int):
    """Opens the Timer sheet and moves its ruler until it reads `target` minute."""
    tap(*TIMER, pause=2.5)
    nodes = dump()
    if read_minutes(nodes) is None:
        raise ForestError("Timer sheet didn't open")
    ry = ruler_y(nodes)
    log.info("timer sheet: %s minute now, ruler at y=%d", read_minutes(nodes), ry)
    for _ in range(25):
        cur = read_minutes(nodes)
        if cur == target:
            log.info("timer set to %s minute", target)
            return nodes
        cur_eff = 5 if cur in (None, 0) else cur
        ticks = (target - cur_eff) / 5.0
        px = max(-300, min(300, int(ticks * 40)))
        if px == 0:
            px = 30 if target > cur_eff else -30
        if px > 0:
            swipe(700, 700 - px, ry, 1200)   # left = more minutes
        else:
            swipe(380, 380 - px, ry, 1200)   # right = fewer minutes
        nodes = dump()
    raise ForestError(f"couldn't set the timer to {target} (stuck at {read_minutes(nodes)})")


# ---------------------------------------------------------------- commands
def cmd_create_room(minutes: int) -> dict:
    if session_running():
        raise ForestError("a session is still running")
    open_forest()
    state = settled_screen()
    if state == "running":
        raise ForestError("a session is running in Forest")
    if state == "room_home":
        log.info("old room still open — closing it first")
        cmd_close_room()
        state = tidy()
    if state != "home":
        raise ForestError(f"Forest isn't on its home screen ({state})")
    nodes = set_timer(int(minutes))
    plant_mode = find(nodes, desc="Plant mode")
    if not plant_mode:
        raise ForestError("Plant mode button not found")
    tap(*center(plant_mode), pause=2.5)
    nodes = dump()
    together = find(nodes, text="Plant Together")
    if not together:
        raise ForestError("Plant Together option not found")
    tap(*center(together), pause=2.5)
    nodes = dump()
    create = find(nodes, text="Create Room")
    if not create:
        raise ForestError("Create Room button not found")
    tap(*center(create), pause=4)
    code = None
    net_error = False
    for _ in range(8):
        nodes = dump()
        n = find(nodes, contains="Room code")
        if n:
            m = re.search(r"Room code\s+([A-Z0-9]{6,12})", n["text"])
            if m:
                code = m.group(1)
                break
        if find(nodes, contains="Something went wrong") or find(nodes, contains="No Internet"):
            net_error = True
            break
        time.sleep(2)
    if not code:
        tidy()
        if net_error or has_internet() is False:
            HEALER.request("Forest couldn't reach its server", network=True)
            raise ForestError("BlueStacks has no internet — Forest couldn't create the room (restarting BlueStacks)")
        raise ForestError("room code didn't appear")
    back(pause=2.5)
    if classify() != "room_home":
        tidy()
    save_state(room_code=code, room_minutes=int(minutes), session_until=None,
               room_created_at=datetime.now().isoformat())
    log.info("room created: %s (%s min)", code, minutes)
    return {"code": code}


def count_members() -> int | None:
    tap(*ROOM_AVATARS, pause=2.5)
    nodes = dump()
    if not find(nodes, contains="Room code"):
        tidy()
        return None
    refresh = next((n for n in nodes if n["click"] and n["b"][0] > 880 and n["b"][1] < 260), None)
    if refresh:
        tap(*center(refresh), pause=2.5)
        nodes = dump()
    count = sum(1 for n in nodes if n["text"] == "Remove")
    back(pause=2)
    return count


def cmd_start_room() -> dict:
    if session_running():
        return {"started": True, "members": load_state().get("members")}
    open_forest()
    state = settled_screen()
    if state != "room_home":
        raise ForestError(f"no open room on screen ({state})")
    members = count_members()
    if settled_screen(looks=3) != "room_home":   # safety: only ever tap Start on the room screen
        raise ForestError("left the room screen unexpectedly")
    tap(*ROOM_START, pause=3.5)
    nodes = dump()
    if find(nodes, contains="at least 2 people"):
        ok = find(nodes, text="OK")
        if ok:
            tap(*center(ok))
        log.info("start refused: fewer than 2 people (members seen: %s)", members)
        return {"started": False, "members": members}
    after = classify()
    if after == "room_home":
        return {"started": False, "members": members, "note": "Start didn't take"}
    minutes = int(load_state().get("room_minutes") or 60)
    until = datetime.now() + timedelta(minutes=minutes, seconds=40)
    save_state(session_until=until.isoformat(), members=members)
    log.info("session started (%s min, members %s) — ends ~%s", minutes, members, until.strftime("%H:%M"))
    threading.Thread(target=finish_session_later, args=(until,), daemon=True).start()
    return {"started": True, "members": members}


def finish_session_later(until: datetime):
    time.sleep(max(0, (until - datetime.now()).total_seconds()))
    with LOCK:
        try:
            for _ in range(10):
                state = classify()
                log.info("after session: %s", state)
                if state in ("home", "room_home"):
                    break
                if state == "running":
                    time.sleep(30)
                    continue
                tidy(max_steps=3)
        except Exception:
            log.exception("finishing the session failed")
        finally:
            save_state(session_until=None)


def cmd_close_room() -> dict:
    if session_running():
        raise ForestError("a session is running — not closing")
    open_forest()
    state = tidy()
    if state != "room_home":
        return {"closed": False, "note": f"no room open ({state})"}
    tap(*ROOM_EXIT, pause=2.5)
    nodes = dump()
    exit_btn = find(nodes, text="Exit")
    if not exit_btn:
        tidy()
        raise ForestError("Exit dialog didn't appear")
    tap(*center(exit_btn), pause=3)
    save_state(room_code=None)
    log.info("room closed")
    return {"closed": True}


def cmd_status() -> dict:
    try:
        adb_connect()
        return {"screen": classify(), "session_running": session_running(), "internet": has_internet()}
    except ForestError as e:
        return {"screen": "error", "error": str(e)}


# ---------------------------------------------------------------- self-healing
def _bluestacks_instance() -> str | None:
    """The Android 11 instance name from bluestacks.conf (e.g. Rvc64)."""
    if os.environ.get("BLUESTACKS_INSTANCE"):
        return os.environ["BLUESTACKS_INSTANCE"]
    for conf in BS_CONF_CANDIDATES:
        try:
            with open(conf, encoding="utf-8", errors="replace") as f:
                text = f.read()
        except OSError:
            continue
        names = sorted(set(re.findall(r'^bst\.instance\.([A-Za-z0-9_]+)\.', text, re.M)))
        for want in ("Rvc64", "Rvc"):
            for n in names:
                if n.startswith(want):
                    return n
        if names:
            return names[0]
    return None


def kill_bluestacks():
    for exe in ("HD-Player.exe",):
        subprocess.run(["taskkill", "/F", "/IM", exe], capture_output=True, timeout=30)
    time.sleep(6)


def launch_bluestacks():
    inst = _bluestacks_instance()
    args = [HD_PLAYER] + (["--instance", inst] if inst else [])
    log.info("starting BlueStacks: %s", " ".join(args))
    flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    subprocess.Popen(args, cwd=BS_DIR, creationflags=flags, close_fds=True)


def bluestacks_running() -> bool:
    try:
        out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq HD-Player.exe"], capture_output=True, text=True, timeout=20).stdout
    except Exception:
        return True  # can't tell — don't act on it
    return "HD-Player.exe" in out


class Healer:
    """Restarts BlueStacks when it froze or lost its internet. Runs restarts in
    the background so a bot command never waits minutes; the bot just retries."""

    def __init__(self):
        self.busy = False
        self.last_heal = 0.0
        self.last_net_heal = 0.0
        self._lock = threading.Lock()

    def can_heal(self, network=False) -> bool:
        now = time.time()
        if session_running():
            return False
        if now - self.last_heal < HEAL_MIN_GAP:
            return False
        if network and now - self.last_net_heal < HEAL_NET_MIN_GAP:
            return False
        return True

    def request(self, reason: str, network=False):
        if self.busy or not self.can_heal(network):
            return False
        threading.Thread(target=self.heal, args=(reason, network), daemon=True).start()
        return True

    def heal(self, reason: str, network=False) -> bool:
        with self._lock:
            if self.busy:
                return False
            self.busy = True
            self.last_heal = time.time()
            if network:
                self.last_net_heal = self.last_heal
        log.warning("restarting BlueStacks — %s", reason)
        try:
            kill_bluestacks()
            launch_bluestacks()
            deadline = time.time() + BOOT_WAIT
            ok = False
            while time.time() < deadline:
                time.sleep(10)
                if "connected" in _adb_connect_once() and android_ready():
                    ok = True
                    break
            if not ok:
                log.error("BlueStacks didn't come back within %ss", BOOT_WAIT)
                return False
            time.sleep(15)  # let the launcher settle
            adb("shell", "monkey", "-p", PKG, "-c", "android.intent.category.LAUNCHER", "1")
            time.sleep(10)
            net = has_internet()
            log.info("BlueStacks is back ✓ (internet: %s)", {True: "yes", False: "NO", None: "unknown"}[net])
            return True
        except Exception:
            log.exception("restarting BlueStacks failed")
            return False
        finally:
            self.busy = False

    def check(self):
        """One watchdog round. Only acts when idle (no command, no session)."""
        if self.busy or session_running() or LOCK.locked():
            return
        if not bluestacks_running():
            self.request("BlueStacks isn't open")
            return
        if "connected" not in _adb_connect_once() or not android_ready():
            time.sleep(30)  # one more look before deciding it's frozen
            if LOCK.locked() or session_running():
                return
            if "connected" not in _adb_connect_once() or not android_ready():
                self.request("BlueStacks stopped answering (frozen?)")
            return
        if room_recently_opened():
            return  # a room is waiting for people — never restart under it for a maybe-network issue
        if has_internet() is False:
            time.sleep(30)
            if has_internet() is False and not LOCK.locked():
                self.request("BlueStacks has no internet", network=True)


HEALER = Healer()


def watchdog_forever():
    time.sleep(20)
    while True:
        try:
            HEALER.check()
        except Exception:
            log.exception("watchdog round failed")
        time.sleep(HEAL_CHECK_EVERY)


def install_startup():
    startup = os.path.join(os.environ.get("APPDATA", ""), r"Microsoft\Windows\Start Menu\Programs\Startup")
    if not os.path.isdir(startup):
        print("Couldn't find the Windows Startup folder:", startup)
        return
    bat = os.path.join(startup, "forest_host_helper.bat")
    with open(bat, "w", encoding="utf-8") as f:
        f.write("@echo off\r\n")
        f.write("title Forest auto-host helper\r\n")
        f.write(f'cd /d "{HERE}"\r\n')
        f.write("timeout /t 30 /nobreak >nul\r\n")  # give Windows + Wi-Fi a moment
        f.write(f'"{sys.executable}" host_agent.py\r\n')
    print("Done. The helper will now start by itself every time you log in to Windows:")
    print("  ", bat)
    print("(It also opens BlueStacks by itself if it isn't open.) To undo, delete that file.")


LOCK = threading.Lock()
ACTIONS = {
    "create_room": lambda p: cmd_create_room(int(p.get("minutes") or 60)),
    "start_room": lambda p: cmd_start_room(),
    "close_room": lambda p: cmd_close_room(),
    "status": lambda p: cmd_status(),
}


def _wait_for_session_end(max_wait=150):
    """Back-to-back blocks: the next room is requested the same minute the
    previous session ends. If it ends within ~2.5 min, wait for it (and for
    Continue -> All Done) instead of refusing."""
    until = load_state().get("session_until")
    if not until:
        return
    left = (datetime.fromisoformat(until) - datetime.now()).total_seconds()
    if 0 < left <= max_wait:
        log.info("previous session ends in %ds — waiting for it before preparing the next room", int(left))
        time.sleep(left + 2)


def run_action(action: str, params: dict) -> dict:
    fn = ACTIONS.get(action)
    if not fn:
        return {"ok": False, "error": f"unknown action {action}"}
    if action == "create_room":
        _wait_for_session_end()  # the finisher then holds LOCK until Forest is back home
    with LOCK:
        try:
            adb_connect()
            result = fn(params)
            return {"ok": True, **result}
        except ForestError as e:
            log.warning("%s failed: %s", action, e)
            return {"ok": False, "error": str(e)}
        except Exception as e:
            log.exception("%s crashed", action)
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}


# ---------------------------------------------------------------- bot link
def get_secret() -> str:
    if os.path.exists(SECRET_FILE):
        with open(SECRET_FILE, encoding="utf-8") as f:
            s = f.read().strip()
        if s:
            return s
    s = secrets.token_urlsafe(24)
    with open(SECRET_FILE, "w", encoding="utf-8") as f:
        f.write(s)
    print("\n" + "=" * 60)
    print("NEW SECRET created in secret.txt:\n")
    print("   " + s)
    print("\nPut this exact value in Railway as the variable HOST_AGENT_SECRET.")
    print("=" * 60 + "\n")
    return s


async def serve_forever():
    import websockets

    secret = get_secret()
    delay = 3
    while True:
        try:
            async with websockets.connect(BOT_WS_URL, ping_interval=20, ping_timeout=20, max_size=8_000_000) as ws:
                await ws.send(json.dumps({"type": "agent_hello", "secret": secret, "version": VERSION}))
                log.info("connected to the bot, waiting for commands")
                delay = 3
                async for raw in ws:
                    try:
                        msg = json.loads(raw)
                    except ValueError:
                        continue
                    if not isinstance(msg, dict):
                        continue
                    t = msg.get("type")
                    if t == "agent_denied":
                        log.error("the bot REJECTED this helper: secret.txt doesn't match HOST_AGENT_SECRET in Railway")
                        await asyncio.sleep(60)
                        break
                    if t == "agent_ok":
                        log.info("bot accepted this helper ✓")
                    if t != "cmd":
                        continue  # leaderboard broadcasts etc.
                    log.info("command: %s %s", msg.get("action"), {k: v for k, v in msg.items() if k not in ("type", "id", "action")})
                    result = await asyncio.to_thread(run_action, msg.get("action"), msg)
                    await ws.send(json.dumps({"type": "cmd_result", "id": msg.get("id"), **result}))
                    log.info("result: %s", result)
        except Exception as e:
            log.warning("connection problem: %s — retrying in %ss", e, delay)
        await asyncio.sleep(delay)
        delay = min(delay * 2, 60)


def main():
    print(f"Forest auto-host helper v{VERSION} — BlueStacks: {ADB} -> {DEVICE}")
    if "--screen" in sys.argv:
        adb_connect()
        open_forest()
        print("Forest screen:", classify())
        return
    if "--install-startup" in sys.argv:
        install_startup()
        return
    if "--health" in sys.argv:
        print("BlueStacks open:", bluestacks_running())
        print("ADB:", _adb_connect_once().strip()[:80])
        print("Android ready:", android_ready())
        print("Internet inside BlueStacks:", has_internet())
        print("Instance:", _bluestacks_instance())
        return
    if "--heal" in sys.argv:
        print("Restarting BlueStacks and waiting until it's ready (up to ~4 min)...")
        print("OK ✓" if HEALER.heal("manual --heal") else "FAILED — see agent.log")
        return
    if "--selftest" in sys.argv:
        print("Selftest: opening a 10-minute room...")
        r = run_action("create_room", {"minutes": 10})
        print(r)
        if r.get("ok"):
            print(f"Join link: https://forestapp.cc/join-room?token={r['code']}")
            time.sleep(5)
            print("Closing it:", run_action("close_room", {}))
        return
    threading.Thread(target=watchdog_forever, daemon=True).start()
    asyncio.run(serve_forever())


if __name__ == "__main__":
    main()
