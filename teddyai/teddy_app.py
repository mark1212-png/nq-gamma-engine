"""
teddy_app.py -- TeddyAI as a desktop window.

    python teddy_app.py            normal
    python teddy_app.py --sim      tick feed and teddychart on synthetic tapes (no key)
    python teddy_app.py --no-chart without teddychart (the terminal's built-in chart)
    python teddy_app.py --no-opra  without the equity open-interest fetch
    python teddy_app.py --attach   don't start anything; just open a window on what's running

What this is, and what it is not. It is start_teddy.ps1's job done in Python, with
the browser replaced by a native window: the same services in the same order
(teddy.py :5137, live_feed.py, teddychart :5138, then the equity open-interest
fetch), the same "a port that already answers is left alone" rule, the same
-Sim / -NoChart / -NoOpra switches. Nothing here touches
the pipeline, teddy.py, or command_center.py -- the window points at
http://127.0.0.1:5137/cc and the page behaves exactly as it does in Chrome.

What it adds over the .bat:
    * one window instead of four consoles and a browser tab
    * one instance: launching it again raises the window that is already open
    * a tray icon; closing the window minimises there rather than stopping the day
    * Win+Alt+T raises the terminal, Win+Alt+C the chart, Win+Alt+G the ladder
    * the chart and the gamma ladder tear out into windows of their own, from the
      pop icon on each. Drag one back over the terminal and let go and it docks,
      returning to the slot it came from; closing it does the same thing, so a view
      is never stranded behind a window that is gone.

The tear-out is cheap on purpose. teddychart is already its own server on :5138,
so its window is a second VIEW of one stream. The ladder window is the terminal
itself at /cc?solo=ladder -- same page, same build, tabs and pipeline controls
hidden, and boot() skips the pipeline run, so opening a window never triggers a
rebuild. Nothing is recomputed and no second connection is opened.

Requires: pywebview. Optional: pystray + pillow for the tray icon (without them
the window still works, close just exits). Install with
    pip install pywebview pystray pillow

The Databento key is read by the services themselves from DATABENTO_API_KEY, as
before. This file never reads it, never sends it anywhere, and makes no API calls.
"""

from __future__ import annotations

import argparse
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent

DASH_PORT = 5137          # teddy.py
CHART_PORT = 5138         # teddychart/server.py
LOCK_PORT = 5139          # this launcher's single-instance lock and its doorbell

DASH_URL = f"http://127.0.0.1:{DASH_PORT}/cc"
CHART_URL = f"http://127.0.0.1:{CHART_PORT}/"

# Views that can run in a window of their own.
#   chart  -- teddychart's own server. A second window is a second VIEW of one
#             stream, not a second stream.
#   ladder -- the terminal itself with ?solo=ladder: same page, same build, tabs
#             and the pipeline controls hidden, and boot() skips the run so opening
#             a window never kicks off a rebuild.
SOLO_URLS = {
    "chart": CHART_URL,
    "ladder": f"http://127.0.0.1:{DASH_PORT}/cc?solo=ladder",
}
SOLO_TITLES = {"chart": "TeddyAI Chart", "ladder": "TeddyAI Gamma Ladder"}
SOLO_SIZE = {"chart": (1200, 820), "ladder": (1080, 900)}

# processes this launcher started itself. Services that were already up when we
# arrived are somebody else's -- we never stop those.
_OWNED: list[subprocess.Popen] = []
_window = None
_solo: dict = {}          # view -> its window, while it is detached
_tray = None


# ---------------------------------------------------------------- small helpers

def say(msg: str) -> None:
    print(f"  {msg}", flush=True)


def port_open(port: int, host: str = "127.0.0.1", timeout: float = 0.35) -> bool:
    with socket.socket() as s:
        s.settimeout(timeout)
        return s.connect_ex((host, port)) == 0


def wait_for(port: int, seconds: float = 45.0) -> bool:
    deadline = time.time() + seconds
    while time.time() < deadline:
        if port_open(port):
            return True
        time.sleep(0.4)
    return False


def feed_alive() -> bool:
    """Is live_feed.py already streaming? It has no port, so freshness of its
    status file is the test -- same rule start_teddy.ps1 uses."""
    import datetime as _dt
    import json
    fs = ROOT / "data" / "bars" / "feed_status.json"
    try:
        doc = json.loads(fs.read_text(encoding="utf-8"))
        stamp = str(doc.get("updated", "")).replace("Z", "+00:00")
        when = _dt.datetime.fromisoformat(stamp)
        if when.tzinfo is None:
            when = when.replace(tzinfo=_dt.timezone.utc)
        age = (_dt.datetime.now(_dt.timezone.utc) - when).total_seconds()
        return age < 90 and doc.get("state") in ("streaming", "connecting")
    except Exception:
        return False


def spawn(args: list[str], cwd: Path, log_name: str | None = None) -> subprocess.Popen:
    """Start a service with no console window of its own. Its stdout goes to a log
    file in notes/ so a crash is still readable after the fact.

    log_name overrides the default (the script's stem). autopull.py needs it: it
    writes notes/autopull.log itself, line by line, and a second open handle
    appending to the same file interleaves with those writes."""
    logs = ROOT / "notes"
    logs.mkdir(exist_ok=True)
    log = open(logs / f"{log_name or Path(args[1]).stem}.log", "a", buffering=1, encoding="utf-8", errors="replace")
    log.write(f"\n---- {time.strftime('%Y-%m-%d %H:%M:%S')} started by teddy_app ----\n")
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    p = subprocess.Popen(args, cwd=str(cwd), stdout=log, stderr=subprocess.STDOUT, creationflags=flags)
    _OWNED.append(p)
    return p


# ---------------------------------------------------------- single instance / IPC

def claim_single_instance() -> socket.socket | None:
    """Bind the lock port. Returns the listening socket if we are the first
    instance, or None if another one already holds it (in which case we have
    rung its doorbell and should exit)."""
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0)
    try:
        srv.bind(("127.0.0.1", LOCK_PORT))
    except OSError:
        srv.close()
        try:
            with socket.create_connection(("127.0.0.1", LOCK_PORT), timeout=2) as c:
                c.sendall(b"SHOW\n")
        except OSError:
            # the port is held by something that is not us. Say so rather than
            # starting a second copy of everything.
            say(f"port {LOCK_PORT} is in use by another program; not starting.")
        return None
    srv.listen(4)
    return srv


def doorbell_loop(srv: socket.socket) -> None:
    """A second launch, or the tray, asks the running instance to show itself."""
    while True:
        try:
            conn, _ = srv.accept()
        except OSError:
            return
        with conn:
            try:
                msg = conn.recv(32).strip().upper()
            except OSError:
                continue
        if msg == b"SHOW":
            show_window()
        elif msg in (b"CHART", b"LADDER"):
            open_solo(msg.decode().lower())


# ------------------------------------------------------------------ the services

def start_services(sim: bool, no_chart: bool, no_feed: bool, no_opra: bool = False) -> None:
    has_key = bool(os.environ.get("DATABENTO_API_KEY"))
    py = sys.executable

    for d in ("data", "data/bars", "notes", "sheets"):
        (ROOT / d).mkdir(parents=True, exist_ok=True)

    # 1. the dashboard. Everything else reads its /api/bars, so it goes first.
    if port_open(DASH_PORT):
        say(f"dashboard   already running on :{DASH_PORT}")
    else:
        if sim:
            os.environ["CC_TICKS"] = "sim"
        say(f"dashboard   starting teddy.py on :{DASH_PORT}")
        spawn([py, "teddy.py"], ROOT)
        if not wait_for(DASH_PORT, 45):
            say(f"teddy.py did not answer on :{DASH_PORT} within 45 s -- see notes/teddy.log")
            sys.exit(1)

    # 2. the live bar feed. It has no port to test, so freshness of
    # data/bars/feed_status.json is the "already running" check -- the same one
    # start_teddy.ps1 makes. Without it a second live_feed.py ends up writing the
    # same day's bar cache as the first, which is worse than having none.
    if no_feed:
        say("live feed   skipped (--no-feed)")
    elif not has_key:
        say("live feed   skipped (no DATABENTO_API_KEY); the dashboard tops the cache up itself")
    elif feed_alive():
        say("live feed   already streaming (feed_status.json is fresh)")
    else:
        say("live feed   starting live_feed.py")
        spawn([py, "live_feed.py"], ROOT)

    # 3. teddychart
    chart_dir = ROOT / "teddychart"
    if no_chart:
        say("teddychart  skipped (--no-chart)")
    elif not (chart_dir / "server.py").exists():
        say("teddychart  not installed (teddychart/server.py missing)")
    elif port_open(CHART_PORT):
        say(f"teddychart  already running on :{CHART_PORT}")
    elif sim:
        say("teddychart  starting on the simulator (--sim)")
        spawn([py, "server.py", "--sim"], chart_dir)
    elif not has_key:
        say("teddychart  skipped (no DATABENTO_API_KEY; --sim runs it on a synthetic tape)")
    else:
        say(f"teddychart  starting server.py on :{CHART_PORT}")
        spawn([py, "server.py"], chart_dir)

    # 4. the equity books, with open interest replayed off the OPRA live gateway --
    # the only place this morning's OI can be read, since the historical endpoint
    # serves complete days only and would hand back the previous close. A real fetch
    # (~320 MB over the four roots, 20+ minutes with the American pass), so nothing
    # waits on it: the Cross-Reference panel picks the chains up when they land.
    # OPRA publishes OI once at 10:30Z and the gateway will not replay a start in the
    # future or older than 24 h, so the window is 10:30Z..00:00Z. job_opra_live
    # re-checks that itself and skips any root whose OI is already current for the
    # settle, which is what makes a second launch cheap.
    if no_opra:
        say("equity OI   skipped (--no-opra)")
    elif not has_key:
        say("equity OI   skipped (no DATABENTO_API_KEY)")
    else:
        import datetime as _dt
        utc = _dt.datetime.now(_dt.timezone.utc)
        if utc.hour + utc.minute / 60.0 < 10.5:
            say(f"equity OI   skipped ({utc:%H:%M}Z is before the 10:30Z open-interest wave)")
        else:
            say("equity OI   starting autopull.py --job opra-live (minutes, not seconds)")
            spawn([py, "autopull.py", "--job", "opra-live"], ROOT, log_name="opra-live")


def stop_owned() -> None:
    for p in _OWNED:
        if p.poll() is None:
            try:
                p.terminate()
            except OSError:
                pass
    deadline = time.time() + 5
    for p in _OWNED:
        try:
            p.wait(timeout=max(0.1, deadline - time.time()))
        except Exception:
            try:
                p.kill()
            except OSError:
                pass


# -------------------------------------------------------------------- the window

def show_window() -> None:
    if _window is None:
        return
    try:
        _window.show()
        _window.restore()
    except Exception:
        pass


def tell_page(js: str) -> None:
    """Say something to the terminal window's page. Harmless if it isn't up yet."""
    if _window is None:
        return
    try:
        _window.evaluate_js(js)
    except Exception:
        pass


def open_solo(view: str) -> bool:
    """Give a view its own window. Returns False if it can't be opened."""
    import webview
    if view not in SOLO_URLS:
        return False
    w = _solo.get(view)
    if w is not None:
        try:
            w.show()
            w.restore()
            return True
        except Exception:
            _solo.pop(view, None)
    if view == "chart" and not port_open(CHART_PORT):
        say("teddychart is not running; nothing to pop out.")
        return False

    width, height = SOLO_SIZE[view]
    w = webview.create_window(SOLO_TITLES[view], SOLO_URLS[view],
                              width=width, height=height, min_size=(560, 420))
    _solo[view] = w

    def _closed(v=view):
        # closing the solo window is the same as docking it: the panel comes back
        # into the terminal, so a view is never lost behind a window that is gone.
        _solo.pop(v, None)
        tell_page(f"window.ccDetached && window.ccDetached({v!r}, false)")
    w.events.closed += _closed
    return True


def close_solo(view: str) -> None:
    w = _solo.pop(view, None)
    if w is None:
        return
    try:
        w.destroy()
    except Exception:
        pass


# ------------------------------------------------- drag a window back to dock it

def start_drag_watch() -> None:
    """Drag a torn-out window back over the terminal and let go: it docks, and the
    panel returns to the slot it came from.

    Windows gives no event for "you dropped my window on top of that one", so this
    watches for the gesture instead: a solo window that has MOVED while the left
    button is down, with the cursor inside the terminal's frame when the button
    comes up. Both halves are required, so parking a window next to the terminal or
    clicking through it never docks anything by accident. Docking is just closing
    the solo window -- the same path its own close button takes.

    Windows lookup is by title rather than by poking at pywebview's native widget,
    so it does not depend on which GUI backend pywebview picked."""
    if os.name != "nt":
        return

    import ctypes
    from ctypes import wintypes

    u32 = ctypes.windll.user32

    class RECT(ctypes.Structure):
        _fields_ = [("left", wintypes.LONG), ("top", wintypes.LONG),
                    ("right", wintypes.LONG), ("bottom", wintypes.LONG)]

    class POINT(ctypes.Structure):
        _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]

    ENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def hwnds_for(titles: set) -> dict:
        out: dict = {}
        buf = ctypes.create_unicode_buffer(300)

        def cb(h, _l):
            if u32.IsWindowVisible(h) and u32.IsWindow(h):
                u32.GetWindowTextW(h, buf, 300)
                t = buf.value
                if t in titles and t not in out:
                    out[t] = h
            return True

        u32.EnumWindows(ENUMPROC(cb), 0)
        return out

    def rect_of(h):
        r = RECT()
        return r if u32.GetWindowRect(h, ctypes.byref(r)) else None

    def cursor():
        p = POINT()
        u32.GetCursorPos(ctypes.byref(p))
        return p

    def button_down() -> bool:
        return bool(u32.GetAsyncKeyState(0x01) & 0x8000)

    def inside(r, p, pad: int = 0) -> bool:
        return (r.left + pad) <= p.x <= (r.right - pad) and (r.top + pad) <= p.y <= (r.bottom - pad)

    seen: dict = {}      # view -> (rect tuple, moved)
    hint = [None]        # the view currently hovering over the terminal

    def set_hint(view):
        if hint[0] == view:
            return
        hint[0] = view
        if view:
            label = "the chart" if view == "chart" else "the gamma ladder"
            tell_page(f"window.ccDropHint && window.ccDropHint({label!r})")
        else:
            tell_page("window.ccDropHint && window.ccDropHint(null)")

    def loop():
        while True:
            time.sleep(0.12)
            try:
                views = list(_solo.keys())
                if not views:
                    seen.clear()
                    set_hint(None)
                    continue

                wanted = {SOLO_TITLES[v] for v in views} | {"TeddyAI"}
                hs = hwnds_for(wanted)
                main = hs.get("TeddyAI")
                main_r = rect_of(main) if main else None
                held = button_down()
                pos = cursor()
                hovering = None

                for v in views:
                    h = hs.get(SOLO_TITLES[v])
                    if not h:
                        seen.pop(v, None)
                        continue
                    r = rect_of(h)
                    if not r:
                        continue
                    now = (r.left, r.top, r.right, r.bottom)
                    prev, moved = seen.get(v, (now, False))
                    if held:
                        moved = moved or (now != prev)
                        # a 12px inset so a window merely touching the terminal's
                        # edge is not a drop
                        if moved and main_r and inside(main_r, pos, 12):
                            hovering = v
                    elif moved:
                        # button released. Drop it if it came to rest over the terminal.
                        if main_r and inside(main_r, pos, 12):
                            set_hint(None)
                            close_solo(v)
                            show_window()
                            seen.pop(v, None)
                            continue
                        moved = False
                    seen[v] = (now, moved)

                set_hint(hovering)
            except Exception:
                # never let the watcher take the app down; it is a convenience
                set_hint(None)
                time.sleep(0.5)

    threading.Thread(target=loop, daemon=True).start()


class Api:
    """Exposed to the page as window.pywebview.api. The page shows its detach
    controls only when this exists, so the same file still works in a browser."""

    def detach(self, view: str) -> bool:
        return open_solo(str(view))

    def dock(self, view: str) -> bool:
        close_solo(str(view))
        return True

    def detached(self) -> list:
        return sorted(_solo.keys())


def build_tray() -> None:
    """Tray icon. Optional -- if pystray/pillow are missing we simply don't have one."""
    global _tray
    try:
        import pystray
        from PIL import Image, ImageDraw
    except ImportError:
        say("tray        pystray/pillow not installed -- no tray icon (pip install pystray pillow)")
        return

    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse((6, 6, 26, 26), fill=(158, 126, 103, 255))     # brass, the sheet's accent
    d.ellipse((38, 6, 58, 26), fill=(158, 126, 103, 255))
    d.ellipse((10, 18, 54, 60), fill=(57, 56, 53, 255))

    def _quit(icon, _item):
        icon.stop()
        try:
            import webview
            for w in list(webview.windows):
                w.destroy()
        except Exception:
            pass

    menu = pystray.Menu(
        pystray.MenuItem("Open TeddyAI", lambda i, x: show_window(), default=True),
        pystray.MenuItem("Pop out the chart", lambda i, x: open_solo("chart")),
        pystray.MenuItem("Pop out the gamma ladder", lambda i, x: open_solo("ladder")),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Quit", _quit),
    )
    _tray = pystray.Icon("teddyai", img, "TeddyAI", menu)
    threading.Thread(target=_tray.run, daemon=True).start()


def register_hotkeys() -> None:
    """Win+Alt+T raises the terminal, Win+Alt+C the chart, Win+Alt+G the ladder.
    Straight ctypes so this needs no extra package and no elevation."""
    if os.name != "nt":
        return
    import ctypes
    from ctypes import wintypes

    MOD_ALT, MOD_WIN, MOD_NOREPEAT = 0x0001, 0x0008, 0x4000
    mods = MOD_ALT | MOD_WIN | MOD_NOREPEAT

    keys = {1: ("T", 0x54), 2: ("C", 0x43), 3: ("G", 0x47)}

    def loop():
        u32 = ctypes.windll.user32
        for hid, (name, vk) in keys.items():
            if not u32.RegisterHotKey(None, hid, mods, vk):
                say(f"hotkey      Win+Alt+{name} is taken by another program -- skipped")
        msg = wintypes.MSG()
        while u32.GetMessageW(ctypes.byref(msg), None, 0, 0) != 0:
            if msg.message == 0x0312:  # WM_HOTKEY
                if msg.wParam == 1:
                    show_window()
                elif msg.wParam == 2:
                    open_solo("chart")
                elif msg.wParam == 3:
                    open_solo("ladder")

    threading.Thread(target=loop, daemon=True).start()


# ------------------------------------------------------------------------- main

def main() -> None:
    ap = argparse.ArgumentParser(description="TeddyAI in a desktop window.")
    ap.add_argument("--sim", action="store_true", help="tick feed and teddychart on synthetic tapes")
    ap.add_argument("--no-chart", action="store_true", help="don't start teddychart")
    ap.add_argument("--no-feed", action="store_true", help="don't start live_feed.py")
    ap.add_argument("--no-opra", action="store_true",
                    help="don't start the equity open-interest fetch (autopull.py --job opra-live)")
    ap.add_argument("--attach", action="store_true", help="start nothing; open a window on what is already up")
    args = ap.parse_args()

    try:
        import webview
    except ImportError:
        print("\n  pywebview is not installed.\n  pip install pywebview pystray pillow\n")
        sys.exit(1)

    lock = claim_single_instance()
    if lock is None:
        say("TeddyAI is already open -- raised its window.")
        return
    threading.Thread(target=doorbell_loop, args=(lock,), daemon=True).start()

    print("\n  TeddyAI  desktop\n")
    if args.attach:
        if not port_open(DASH_PORT):
            say(f"nothing is answering on :{DASH_PORT}; run without --attach to start it.")
            sys.exit(1)
        say(f"attached    :{DASH_PORT}")
    else:
        start_services(sim=args.sim, no_chart=args.no_chart, no_feed=args.no_feed,
                       no_opra=args.no_opra)

    global _window
    _window = webview.create_window(
        "TeddyAI", DASH_URL,
        width=1600, height=1000, min_size=(1100, 700),
        text_select=True, confirm_close=False,
        js_api=Api(),
    )

    def on_closing():
        # close means "put it away", not "stop the day" -- but only when there is
        # a tray icon to get it back from.
        if _tray is not None:
            _window.hide()
            return False
        return True
    _window.events.closing += on_closing

    build_tray()
    register_hotkeys()
    start_drag_watch()
    say(f"window      {DASH_URL}")
    say("detach      the pop icon on the chart row, and on the Gamma Ladder header")
    say("dock        drag a torn-out window back over the terminal and let go")
    say("hotkeys     Win+Alt+T terminal · Win+Alt+C chart · Win+Alt+G ladder")
    print()

    try:
        webview.start()
    finally:
        if _tray is not None:
            try:
                _tray.stop()
            except Exception:
                pass
        stop_owned()
        try:
            lock.close()
        except OSError:
            pass


if __name__ == "__main__":
    main()
