"""Offline tests for forest_host/host_agent.py (the PC helper) — no BlueStacks,
no Windows needed. A fake BlueStacks answers ADB/tasklist calls.

Run:  python tests/test_host_agent_offline.py
"""
import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
import time
import types
from datetime import datetime, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP = tempfile.mkdtemp(prefix="hostagent_")
shutil.copy(os.path.join(ROOT, "forest_host", "host_agent.py"), TMP)

spec = importlib.util.spec_from_file_location("host_agent", os.path.join(TMP, "host_agent.py"))
H = importlib.util.module_from_spec(spec)
spec.loader.exec_module(H)

PASSED = FAILED = 0


def check(name, cond, detail=""):
    global PASSED, FAILED
    if cond:
        PASSED += 1
        print("  ✓", name)
    else:
        FAILED += 1
        print("  ✗", name, detail)


class FakeBlueStacks:
    def __init__(self):
        self.reset()

    def reset(self):
        self.running = True        # HD-Player.exe process exists
        self.adb_ok = True         # adb connect works
        self.booted = True
        self.ping = "ok"           # ok | fail | none
        self.kills = 0
        self.launches = []
        self.recover_on_launch = True
        self.screen_texts = []
        self.taps = []
        self.cmds = []

    # subprocess.run replacement
    def run(self, args, **kw):
        a = [str(x) for x in args]
        out = ""
        if a[0] == "taskkill":
            self.kills += 1
            self.running = False
            self.adb_ok = False
        elif a[0] == "tasklist":
            out = "HD-Player.exe   1234 Console" if self.running else "INFO: No tasks are running"
        elif a[1:2] == ["connect"]:
            out = "connected to 127.0.0.1:5555" if self.adb_ok else "cannot connect to 127.0.0.1:5555: No connection"
        elif a[1:2] == ["disconnect"]:
            out = "disconnected"
        elif "-s" in a:
            cmd = " ".join(a[3:])
            self.cmds.append(cmd)
            if not self.adb_ok:
                out = "error: device offline"
            elif "getprop sys.boot_completed" in cmd:
                out = "1\n" if self.booted else "\n"
            elif "ping" in cmd:
                out = {"ok": "1 packets transmitted, 1 received, 0% packet loss",
                       "fail": "1 packets transmitted, 0 received, 100% packet loss",
                       "none": "ping: not found"}[self.ping]
            elif "input tap" in cmd:
                self.taps.append(cmd)
        if kw.get("text"):
            return types.SimpleNamespace(stdout=out, stderr="", returncode=0)
        return types.SimpleNamespace(stdout=out.encode(), stderr=b"", returncode=0)

    def popen(self, args, **kw):
        self.launches.append([str(x) for x in args])
        self.running = True
        if self.recover_on_launch:
            self.adb_ok = True
            self.booted = True
        return types.SimpleNamespace(pid=1)


ORIG_CLASSIFY = H.classify
ORIG_TIDY = H.tidy
BS = FakeBlueStacks()
H.subprocess.run = BS.run
H.subprocess.Popen = BS.popen
H.time.sleep = lambda s: None
H.STATE_FILE = os.path.join(TMP, "agent_state.json")
H.BS_CONF_CANDIDATES = [os.path.join(TMP, "bluestacks.conf")]
H.BOOT_WAIT = 60

# Threads run inline so results are deterministic.
class InlineThread:
    def __init__(self, target, args=(), daemon=None):
        self.target, self.args = target, args

    def start(self):
        self.target(*self.args)


H.threading.Thread = InlineThread


def fresh():
    BS.reset()
    H.HEALER = H.Healer()
    H.PING_SEEN_OK = False
    if os.path.exists(H.STATE_FILE):
        os.remove(H.STATE_FILE)


print("\n[1] BlueStacks frozen: ADB can't connect")
fresh()
BS.adb_ok = False
try:
    H.adb_connect()
    check("adb_connect raises", False)
except H.ForestError as e:
    check("adb_connect raises a clear error", "restarting BlueStacks" in str(e))
check("BlueStacks was killed", BS.kills == 1)
check("BlueStacks was relaunched", len(BS.launches) == 1)
check("healer is idle again", H.HEALER.busy is False)
check("ADB works after the restart", "connected" in H._adb_connect_once())
check("Forest reopened after the restart", any("monkey" in c and "cc.forestapp" in c for c in BS.cmds))

print("\n[2] Rate limit: no second restart within 10 minutes")
BS.adb_ok = False
try:
    H.adb_connect()
except H.ForestError:
    pass
check("no second kill", BS.kills == 1)
H.HEALER.last_heal -= H.HEAL_MIN_GAP + 1
BS.adb_ok = False
try:
    H.adb_connect()
except H.ForestError:
    pass
check("restarts again once 10 min have passed", BS.kills == 2)

print("\n[3] While restarting, commands get a clear 'try again' answer")
fresh()
H.HEALER.busy = True
r = H.run_action("create_room", {"minutes": 30})
check("create_room refused, not crashed", r["ok"] is False and "restarting right now" in r["error"])
check("nothing was killed", BS.kills == 0)

print("\n[4] Never restarts during a running session")
fresh()
H.save_state(session_until=(datetime.now() + timedelta(minutes=30)).isoformat())
BS.adb_ok = False
H.HEALER.check()
check("watchdog leaves BlueStacks alone mid-session", BS.kills == 0)
try:
    H.adb_connect()
except H.ForestError:
    pass
check("a failing command mid-session doesn't restart it either", BS.kills == 0)

print("\n[5] Watchdog")
fresh()
H.HEALER.check()
check("healthy → does nothing", BS.kills == 0 and not BS.launches)
fresh()
BS.running = False
BS.adb_ok = False
H.HEALER.check()
check("BlueStacks closed → opens it", len(BS.launches) == 1)
fresh()
BS.adb_ok = False
H.HEALER.check()
check("frozen on both looks → restarts", BS.kills == 1 and len(BS.launches) == 1)
fresh()
calls = {"n": 0}
orig = H._adb_connect_once


def flaky():
    calls["n"] += 1
    return "cannot connect" if calls["n"] == 1 else "connected to 127.0.0.1:5555"


H._adb_connect_once = flaky
H.HEALER.check()
H._adb_connect_once = orig
check("one hiccup that recovers → no restart", BS.kills == 0)
fresh()
H.LOCK.acquire()
BS.adb_ok = False
H.HEALER.check()
H.LOCK.release()
check("busy with a bot command → doesn't interfere", BS.kills == 0)

print("\n[6] Internet check")
fresh()
BS.ping = "none"
check("no ping tool → unknown", H.has_internet() is None)
BS.ping = "fail"
check("ping fails before ever working → unknown (some emulators block ping)", H.has_internet() is None)
BS.ping = "ok"
check("ping works → True", H.has_internet() is True)
BS.ping = "fail"
check("ping fails after it worked → False", H.has_internet() is False)
H.HEALER.check()
check("watchdog: no internet → restarts BlueStacks", BS.kills == 1)
BS.ping = "fail"
H.HEALER.last_heal -= H.HEAL_MIN_GAP + 1
H.HEALER.check()
check("…but no-internet restarts at most every 2 h", BS.kills == 1)

print("\n[7] Never restarts under a room that's waiting for people")
fresh()
H.PING_SEEN_OK = True
BS.ping = "fail"
H.save_state(room_code="ABCD1234", room_created_at=datetime.now().isoformat())
H.HEALER.check()
check("open room (made 2 min ago) → left alone", BS.kills == 0)
H.save_state(room_created_at=(datetime.now() - timedelta(minutes=45)).isoformat())
H.HEALER.check()
check("old room (45 min) → network restart allowed", BS.kills == 1)

print("\n[8] Forest error dialogs are recognised, not mistaken for a running session")
fresh()
H.screen_pixels = lambda: (1080, 1920, 16, b"")


def fake_dump_factory(texts):
    def _dump():
        return [{"text": t, "desc": "", "click": True, "b": (400, 900, 680, 1000)} for t in texts]
    return _dump


H.dump = fake_dump_factory(["Something went wrong. Error code: 0", "OK"])
check("'Something went wrong' → error_dialog", H.classify() == "error_dialog")
H.dump = fake_dump_factory(["No Internet Connection", "Please connect to the Internet and try again", "OK"])
check("'No Internet Connection' → no_internet_dialog", H.classify() == "no_internet_dialog")

print("\n[9] Create Room hits a network error → clear message + BlueStacks restart")
fresh()
seq = {"i": 0}
screens = [
    ["Plant mode"],                          # set_timer result (patched below)
]
H.open_forest = lambda: None
states = iter(["home", "home"])
H.tidy = lambda max_steps=6: next(states, "home")
H.set_timer = lambda m: [{"text": "", "desc": "Plant mode", "click": True, "b": (900, 1200, 1000, 1300)}]
dumps = iter([
    [{"text": "Plant Together", "desc": "", "click": True, "b": (0, 1700, 1080, 1850)}],
    [{"text": "Create Room", "desc": "", "click": True, "b": (0, 1550, 1080, 1650)}],
    [{"text": "Something went wrong. Error code: 0", "desc": "", "click": False, "b": (0, 0, 1, 1)},
     {"text": "OK", "desc": "", "click": True, "b": (400, 900, 680, 1000)}],
])
H.dump = lambda: next(dumps)
r = H.run_action("create_room", {"minutes": 60})
check("create_room fails with a clear 'no internet' reason", r["ok"] is False and "no internet" in r["error"])
check("…and BlueStacks gets restarted", BS.kills == 1 and len(BS.launches) == 1)

print("\n[10] Finds the Android 11 instance in bluestacks.conf")
with open(H.BS_CONF_CANDIDATES[0], "w") as f:
    f.write('bst.instance.Pie64.abi_list="x86"\nbst.instance.Rvc64.abi_list="x86"\nbst.instance.Rvc64.adb_port="5555"\n')
check("picks Rvc64", H._bluestacks_instance() == "Rvc64")
fresh()
H.launch_bluestacks()
check("launches HD-Player.exe --instance Rvc64", BS.launches[-1][-2:] == ["--instance", "Rvc64"] and BS.launches[-1][0].endswith("HD-Player.exe"))
os.remove(H.BS_CONF_CANDIDATES[0])
check("no conf → plain HD-Player.exe", H._bluestacks_instance() is None)

print("\n[11] --install-startup writes a Startup .bat")
appdata = os.path.join(TMP, "AppData")
startup = os.path.join(appdata, r"Microsoft\Windows\Start Menu\Programs\Startup")
os.makedirs(startup)
os.environ["APPDATA"] = appdata
H.install_startup()
bat = os.path.join(startup, "forest_host_helper.bat")
check("bat created", os.path.exists(bat))
content = open(bat).read() if os.path.exists(bat) else ""
check("bat starts host_agent.py from the helper folder", "host_agent.py" in content and H.HERE in content)

print("\n[12] A passing dark overlay isn't mistaken for a running session")
fresh()
H.open_forest = lambda: None
H.count_members = lambda: 1
H.finish_session_later = lambda until: None  # real one runs in a thread later; inline it would wait on LOCK
seq12 = iter(["running", "room_home", "room_home", "room_home"])
H.tidy = lambda max_steps=6: next(seq12, "room_home")
H.classify = lambda: "running" if taps12 else "room_home"
taps12 = []
H.tap = lambda x, y, pause=1.5: taps12.append((x, y))
H.dump = lambda: []
r = H.run_action("start_room", {})
check("one 'running' glimpse, then the room → Start is pressed", r.get("ok") and r.get("started") and H.ROOM_START in taps12, str(r))
fresh()
H.tidy = lambda max_steps=6: "running"
taps12.clear()
r = H.run_action("start_room", {})
check("a real running session (stays dark) → refuses, never taps", r["ok"] is False and "running" in r["error"] and not taps12, str(r))
fresh()
H.save_state(session_until=(datetime.now() + timedelta(minutes=30)).isoformat())
looks = {"n": 0}
def _t(max_steps=6):
    looks["n"] += 1
    return "running"
H.tidy = _t
H.settled_screen()
check("during OUR session, 'running' is believed at once (no re-looks)", looks["n"] == 1)

print("\n[13] Forest Plus ad after a session is closed, not mistaken for a session")


def fake_img(bg=(21, 17, 42), bands=()):
    w, h = 1080, 1920
    raw = bytearray(16) + bytearray(bytes((*bg, 255)) * (w * h))
    raw[0:4] = w.to_bytes(4, "little"); raw[4:8] = h.to_bytes(4, "little")
    for (y1, y2, color) in bands:
        line = bytes((*color, 255)) * w
        for y in range(y1, y2):
            i = 16 + y * w * 4
            raw[i:i + w * 4] = line
    return (w, h, 16, bytes(raw))


promo_img = fake_img(bands=[(1778, 1852, (120, 238, 180))])
running_img = fake_img(bg=(20, 40, 35))
H.dump = lambda: []
H.screen_pixels = lambda: promo_img
H.classify = ORIG_CLASSIFY
check("dark + wide coloured button → promo", H.classify() == "promo")
H.screen_pixels = lambda: running_img
check("plain dark screen → still 'running'", H.classify() == "running")
end_img = fake_img(bands=[(1825, 1856, (255, 255, 255))])
H.screen_pixels = lambda: end_img
check("session end screen (white Continue) is not taken for the ad", H.classify() == "end_screen")
ORIG_HOME_IMG = fake_img(bg=(90, 190, 150), bands=[(1825, 1855, (255, 255, 255))])
H.screen_pixels = lambda: ORIG_HOME_IMG
check("normal Forest home still reads 'home'", H.classify() == "home")
fresh()
taps13, backs13 = [], []
shots = iter([promo_img, promo_img, ORIG_HOME_IMG])
H.screen_pixels = lambda: next(shots, ORIG_HOME_IMG)
H.tap = lambda x, y, pause=1.5: taps13.append((x, y))
H.back = lambda: backs13.append(1)
H.tidy = ORIG_TIDY
st13 = H.tidy()
check("tidy taps the ad's ⓧ (top-right), never the trial button", taps13 and taps13[0] == H.PROMO_CLOSE and all(y < 300 for _, y in taps13))
check("…and ends on the home screen", st13 == "home", st13)

print(f"\n{PASSED} passed, {FAILED} failed")
shutil.rmtree(TMP, ignore_errors=True)
sys.exit(1 if FAILED else 0)
