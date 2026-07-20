"""Watchers — "ping me when this changes" for anything visible on screen.

You point at a screen region (drag a rectangle on a screenshot in the Vision
tab), say what to watch ("the settlement count", "the deploy status pill") and
optionally when to alert ("if it drops", "if it turns red"). COSMOS then polls
that region on an interval:

  1. CHEAP GATE — the region is captured with `screencapture -R` and downscaled
     to a tiny thumbnail; its hash is compared to the last poll. Identical hash
     → nothing moved → no LLM call, costs ~0.
  2. VISION READ — pixels changed → the region image goes to the vision model,
     which reads the CURRENT VALUE/STATE the watcher asks about ("1,284",
     "status: red").
  3. ALERT DECISION — no condition → alert on any value change. With a natural-
     language condition, a tiny text-LLM call judges (prev, current, condition)
     → {alert, reason}.
  4. PING — alert fires as a spoken line + a card in the HUD chat (WS broadcast)
     AND a native macOS notification, so it lands even with the tab closed.

Regions are stored as FRACTIONS of the screen (0..1) so they survive Retina
scale and resolution changes; they're converted to point coords at each capture.
Storage: ~/.friday/watchers.json. One tick loop, per-watcher intervals.
"""
from __future__ import annotations

import os
import re
import json
import time
import base64
import asyncio
import hashlib
import subprocess
from datetime import datetime
from pathlib import Path

from services import llm, vision

FILE = Path.home() / ".friday" / "watchers.json"
SHOTS = Path.home() / ".friday" / "watcher_shots"

MIN_INTERVAL_S = 15
HISTORY_CAP = 30
# Re-alert only when the value moves again — but never more often than this.
ALERT_COOLDOWN_S = 300

# ── URL mode: the watcher owns the page, not the screen. Each poll renders the
# URL in a one-shot HEADLESS Chrome at a FIXED viewport and crops the drawn
# region — so it works with the page in the background, never depends on what's
# on the user's screen, and region fractions map to identical pixels every time.
# The persistent profile keeps logins: sign in once via the SIGN IN button
# (headful window of this profile), then headless polls carry the cookies.
CHROME_BIN = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
WATCH_PROFILE = str(Path.home() / ".friday" / "chrome-watch")
# Laptop-like viewport: previews read naturally and fit on screen. Polls
# capture the top VIEW_H px of the page; SELECT mode pins the page to the top
# so the drawn region always matches what polls will see.
VIEW_W, VIEW_H = 1440, 900
SHOT_PORT = 9224          # poll captures (session uses 9223 — never concurrent)
SHOT_SETTLE_S = 3.5       # post-load wait so SPAs paint their data before the shot
_LOGIN_RE = re.compile(r"sign[ -]?in|log[ -]?in|welcome back|single sign|authenticate", re.I)
_LAST_SHOT_LOGIN_WALL = False   # set by _headless_shot, read by check() right after
_HEADLESS_LOCK = asyncio.Lock()      # one profile → captures are serialized

_task: asyncio.Task | None = None
_broadcast = None          # set by start()
_run_lock: asyncio.Lock | None = None   # main's global run lock (for reflexes)
_check_locks: dict[str, asyncio.Lock] = {}


# ─── storage ──────────────────────────────────────────────────────────────────

def _load() -> list[dict]:
    for attempt in (0, 1):
        try:
            data = json.loads(FILE.read_text())
            return data if isinstance(data, list) else []
        except FileNotFoundError:
            return []
        except Exception:
            if attempt == 0:
                time.sleep(0.05)      # torn read during a concurrent write — retry
    return []


def _save(items: list[dict]) -> None:
    try:
        import tempfile
        FILE.parent.mkdir(parents=True, exist_ok=True)
        # UNIQUE temp per write: a shared tmp name let two writers (backend +
        # any script) truncate each other mid-replace — that's how the watcher
        # file got emptied. mkstemp + replace is safe against any concurrency.
        fd, tmp = tempfile.mkstemp(dir=str(FILE.parent), prefix=".watchers-", suffix=".tmp")
        with os.fdopen(fd, "w") as f:
            f.write(json.dumps(items, indent=2))
        os.replace(tmp, FILE)
    except Exception as e:
        print(f"[watchers] save failed: {e}")


def list_watchers() -> list[dict]:
    """Public list — everything except internal fields."""
    out = []
    for w in _load():
        out.append({k: v for k, v in w.items() if not k.startswith("_")})
    return out


def get(wid: str) -> dict | None:
    return next((w for w in _load() if w.get("id") == wid), None)


def _update(wid: str, **fields) -> None:
    items = _load()
    for w in items:
        if w.get("id") == wid:
            w.update(fields)
            _save(items)
            return
    # id not found — never save: if _load() hit a transient partial read, an
    # unconditional save here would persist an EMPTY list and wipe every watcher.


# ─── screen capture ───────────────────────────────────────────────────────────

async def screen_bounds() -> tuple[int, int]:
    """Main-display size in POINTS (what screencapture -R expects)."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "osascript", "-e",
            'tell application "Finder" to get bounds of window of desktop',
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=8)
        parts = [int(x.strip()) for x in out.decode().strip().split(",")]
        return parts[2], parts[3]
    except Exception:
        return 1512, 982     # sane default; capture still mostly works


async def snap_fullscreen() -> dict:
    """Full-screen screenshot for the region picker. Returns
    {ok, image_b64, media_type, width_pts, height_pts} — the image is the
    downscaled JPEG (fast to ship); the region comes back as fractions so the
    display size never has to match."""
    shot = await vision._take_screenshot(str(SHOTS / "picker.png"))
    if shot is None:
        return {"ok": False, "error": vision.SCREENSHOT_FAILED}
    path, media_type = shot
    w, h = await screen_bounds()
    try:
        img_b64 = base64.standard_b64encode(Path(path).read_bytes()).decode()
    except Exception as e:
        return {"ok": False, "error": str(e)[:120]}
    return {"ok": True, "image_b64": img_b64, "media_type": media_type,
            "width_pts": w, "height_pts": h}


async def _capture_region(w: dict, dest: str) -> str | None:
    """Capture the watcher's region (fractions → point rect) to dest PNG."""
    sw, sh = await screen_bounds()
    r = w.get("region") or {}
    x = max(0, int(r.get("x", 0) * sw))
    y = max(0, int(r.get("y", 0) * sh))
    ww = max(24, int(r.get("w", 1) * sw))
    hh = max(24, int(r.get("h", 1) * sh))
    try:
        os.remove(dest)
    except OSError:
        pass
    proc = await asyncio.create_subprocess_exec(
        "screencapture", "-x", f"-R{x},{y},{ww},{hh}", dest,
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
    await proc.communicate()
    if proc.returncode != 0 or not os.path.exists(dest):
        return None
    return dest


# ── Login borrowing: the watch profile can't BE the user's profile (Chrome
# locks a user-data-dir to one instance), but its session state can be COPIED.
# Both Chromes are the same signed binary, so cookies encrypted with the
# "Chrome Safe Storage" Keychain key decrypt fine in the watch profile. We sync
# Cookies + Local Storage before renders, so previews/polls see the user's
# logged-in sessions — no separate sign-in needed for most sites.
REAL_CHROME = Path.home() / "Library" / "Application Support" / "Google" / "Chrome"
_SYNC_TTL_S = 600
_last_sync = 0.0


def _real_profile_dir() -> Path | None:
    try:
        state = json.loads((REAL_CHROME / "Local State").read_text())
        name = state.get("profile", {}).get("last_used") or "Default"
    except Exception:
        name = "Default"
    p = REAL_CHROME / name
    return p if p.exists() else (REAL_CHROME / "Default"
                                 if (REAL_CHROME / "Default").exists() else None)


def _copy_sqlite(src: Path, dst: Path) -> bool:
    """Copy a (possibly live) sqlite DB. sqlite backup handles a mid-write
    source; plain copy is the fallback."""
    import sqlite3
    import shutil
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        s = sqlite3.connect(f"file:{src}?mode=ro", uri=True, timeout=4)
        d = sqlite3.connect(str(dst))
        with d:
            s.backup(d)
        s.close(); d.close()
        return True
    except Exception:
        try:
            shutil.copy2(src, dst)
            for suffix in ("-journal", "-wal", "-shm"):
                sj = Path(str(src) + suffix)
                if sj.exists():
                    shutil.copy2(sj, Path(str(dst) + suffix))
            return True
        except Exception:
            return False


_COOKIE_KEY = ("host_key", "top_frame_site_key", "name", "path",
               "source_scheme", "source_port")


def _merge_cookies(src_db: Path, dst_db: Path) -> bool:
    """Merge cookies from the user's main profile into the watch profile.
    NEWEST WINS per cookie: a login done inside the interactive preview beats
    older main-profile state, but a FRESH main-profile session replaces a stale
    watch-profile cookie (plain INSERT OR IGNORE let expired sessions shadow
    live ones — pages suddenly rendered as login walls)."""
    import sqlite3
    tmp = dst_db.with_suffix(".src.db")
    if not _copy_sqlite(src_db, tmp):
        return False
    try:
        if not dst_db.exists():
            os.replace(tmp, dst_db)
            return True
        match = " AND ".join(f"s.{c}=cookies.{c}" for c in _COOKIE_KEY)
        con = sqlite3.connect(str(dst_db), timeout=6)
        try:
            con.execute("ATTACH DATABASE ? AS src", (str(tmp),))
            # Drop watch-profile cookies that the main profile has a NEWER copy of…
            con.execute(f"DELETE FROM cookies WHERE EXISTS ("
                        f"SELECT 1 FROM src.cookies s WHERE {match} "
                        f"AND s.creation_utc > cookies.creation_utc)")
            # …then borrow everything not present (survivors keep their slot).
            con.execute("INSERT OR IGNORE INTO cookies SELECT * FROM src.cookies")
            con.commit()
            con.execute("DETACH DATABASE src")
        finally:
            con.close()
        return True
    except Exception as e:
        print(f"[watchers] cookie merge failed: {e}")
        return False
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass


def _sync_profile_state(force: bool = False) -> bool:
    """Borrow the user's live Chrome session state into the watch profile.
    Cookies are MERGED (watch-profile logins win); Local Storage is seeded only
    once — mixing leveldb files after the watch profile has its own state would
    corrupt it. Throttled — polls re-sync at most every _SYNC_TTL_S."""
    global _last_sync
    if not force and time.time() - _last_sync < _SYNC_TTL_S:
        return True
    src = _real_profile_dir()
    if not src:
        return False
    dst = Path(WATCH_PROFILE) / "Default"
    ok = False
    # Cookies live at <profile>/Cookies (older layout) or <profile>/Network/Cookies.
    for rel in ("Cookies", "Network/Cookies"):
        f = src / rel
        if f.exists():
            ok = _merge_cookies(f, dst / rel) or ok
    # Local Storage (SPA auth tokens) — first seed only.
    import shutil
    ls_src, ls_dst = src / "Local Storage", dst / "Local Storage"
    if ls_src.exists() and not ls_dst.exists():
        try:
            shutil.copytree(ls_src, ls_dst)
        except Exception:
            pass
    if ok:
        _last_sync = time.time()
    return ok


def _norm_watch_url(url: str) -> str:
    url = (url or "").strip()
    if url and not url.startswith(("http://", "https://")):
        url = "https://" + url
    return url


def _reap_profile_orphans() -> None:
    """Kill ANY Chrome still holding the watch profile. _HEADLESS_LOCK means we
    never run two profile users ourselves, so a chrome-watch Chrome alive at
    launch time can only be the leftover of a SIGKILL'd backend (start.sh's
    reap is -9, so the process-group cleanup below never ran). One live orphan
    bricks every later launch: Chrome's profile singleton hands the new process
    off to the orphan and the new debug port never comes up — the Vision panel's
    "debug port never came up" failure."""
    try:
        subprocess.run(["pkill", "-9", "-f", f"user-data-dir={WATCH_PROFILE}"],
                       capture_output=True, timeout=5)
    except Exception:
        pass


async def _headless_shot(url: str, dest: str, vw: int = VIEW_W, vh: int = VIEW_H,
                         fresh_login: bool = False) -> str | None:
    """Render `url` in a one-shot headless Chrome (watch profile, fixed viewport)
    and write the screenshot to dest. Serialized — the profile can't be shared.

    Chrome 150's `--headless=new --screenshot` writes the file but does NOT
    always exit, so we never wait on the process: we poll for the screenshot to
    appear and stabilize, then kill the whole process GROUP (a plain kill of the
    launcher leaves children holding the profile lock, bricking later polls)."""
    import signal
    try:
        import httpx
        import websockets
    except Exception:
        return None
    try:
        os.remove(dest)
    except OSError:
        pass
    os.makedirs(WATCH_PROFILE, exist_ok=True)
    async with _HEADLESS_LOCK:
        await asyncio.to_thread(_reap_profile_orphans)
        # Borrow the user's live Chrome logins (cookies + local storage) so the
        # page renders signed-in. Must happen INSIDE the lock — never while a
        # watch-profile Chrome is running.
        try:
            await asyncio.to_thread(_sync_profile_state, fresh_login)
        except Exception as e:
            print(f"[watchers] profile sync failed: {e}")
        # Chrome's one-shot --screenshot fires AT the load event — SPAs paint
        # their data after it, so captures came out blank / mid-hydration (the
        # "region reads a different spot" bug). Drive the same throwaway Chrome
        # over CDP instead: navigate → load event → SHOT_SETTLE_S → ONE shot.
        try:
            proc = await asyncio.create_subprocess_exec(
                CHROME_BIN, "--headless=new", "--disable-gpu", "--hide-scrollbars",
                f"--remote-debugging-port={SHOT_PORT}",
                f"--user-data-dir={WATCH_PROFILE}",
                f"--window-size={vw},{vh}",
                "--no-first-run", "--no-default-browser-check", "about:blank",
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
                start_new_session=True)
        except Exception:
            return None
        ws = None
        try:
            ws_url = None
            async with httpx.AsyncClient(timeout=2) as c:
                for _ in range(40):
                    await asyncio.sleep(0.4)
                    try:
                        targets = (await c.get(
                            f"http://localhost:{SHOT_PORT}/json/list")).json()
                        pages = [t for t in targets if t.get("type") == "page"
                                 and t.get("webSocketDebuggerUrl")]
                        if pages:
                            ws_url = pages[0]["webSocketDebuggerUrl"]
                            break
                    except Exception:
                        continue
            if not ws_url:
                return None
            ws = await websockets.connect(ws_url, max_size=None)
            state = {"id": 0, "loaded": False}

            async def cmd(method: str, params: dict | None = None, timeout: float = 15):
                state["id"] += 1
                mid = state["id"]
                await ws.send(json.dumps({"id": mid, "method": method,
                                          "params": params or {}}))
                deadline = time.monotonic() + timeout
                while True:
                    left = deadline - time.monotonic()
                    if left <= 0:
                        raise asyncio.TimeoutError(method)
                    m = json.loads(await asyncio.wait_for(ws.recv(), left))
                    if m.get("method") == "Page.loadEventFired":
                        state["loaded"] = True
                    if m.get("id") == mid:
                        if m.get("error"):
                            raise RuntimeError(str(m["error"])[:100])
                        return m.get("result", {})

            # Same viewport pinning as the interactive preview — the two
            # pipelines must agree on geometry to the pixel.
            await cmd("Emulation.setDeviceMetricsOverride",
                      {"width": vw, "height": vh, "deviceScaleFactor": 1,
                       "mobile": False})
            await cmd("Page.enable")
            await cmd("Page.navigate", {"url": _norm_watch_url(url)})
            deadline = time.monotonic() + 25
            while not state["loaded"] and time.monotonic() < deadline:
                try:
                    m = json.loads(await asyncio.wait_for(ws.recv(), 1.0))
                    if m.get("method") == "Page.loadEventFired":
                        state["loaded"] = True
                except asyncio.TimeoutError:
                    continue
            await asyncio.sleep(SHOT_SETTLE_S)     # let the SPA paint its data
            # Login-wall detection: an expired session renders a sparse sign-in
            # page — reading a region off that produces garbage. Flag it so the
            # watcher can surface "sign in again" instead of a bogus value.
            global _LAST_SHOT_LOGIN_WALL
            _LAST_SHOT_LOGIN_WALL = False
            try:
                r = await cmd("Runtime.evaluate",
                              {"expression": "(document.body&&document.body.innerText||'').slice(0,800)",
                               "returnByValue": True}, timeout=6)
                body_text = (r.get("result", {}).get("value") or "").strip()
                if len(body_text) < 700 and _LOGIN_RE.search(body_text):
                    _LAST_SHOT_LOGIN_WALL = True
            except Exception:
                pass
            shot = await cmd("Page.captureScreenshot", {"format": "png"}, timeout=20)
            data = shot.get("data", "")
            if not data:
                return None
            Path(dest).write_bytes(base64.standard_b64decode(data))
        except Exception as e:
            print(f"[watchers] cdp capture failed: {str(e)[:100]}")
            return None
        finally:
            if ws is not None:
                try:
                    await ws.close()
                except Exception:
                    pass
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            except Exception:
                pass
            try:
                await asyncio.wait_for(proc.wait(), timeout=6)
            except Exception:
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                except Exception:
                    pass
    if not os.path.exists(dest) or os.path.getsize(dest) < 1000:
        return None
    return dest


async def _crop(png: str, region: dict, vw: int, vh: int, dest: str) -> str | None:
    """Crop region fractions out of a viewport-sized PNG via sips (no Pillow)."""
    x = max(0, int(region.get("x", 0) * vw))
    y = max(0, int(region.get("y", 0) * vh))
    w = max(24, int(region.get("w", 1) * vw))
    h = max(24, int(region.get("h", 1) * vh))
    try:
        proc = await asyncio.create_subprocess_exec(
            "sips", "--cropOffset", str(y), str(x), "-c", str(h), str(w),
            png, "--out", dest,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        await asyncio.wait_for(proc.communicate(), timeout=15)
        if proc.returncode == 0 and os.path.exists(dest):
            return dest
    except Exception:
        pass
    return None


async def _capture_url(w: dict, dest: str) -> str | None:
    """URL-mode capture: headless page render → crop the drawn region."""
    vp = w.get("viewport") or {}
    vw, vh = int(vp.get("w") or VIEW_W), int(vp.get("h") or VIEW_H)
    full = dest.rsplit(".", 1)[0] + ".full.png"
    if await _headless_shot(w.get("url", ""), full, vw, vh) is None:
        return None
    return await _crop(full, w.get("region") or {}, vw, vh, dest)


async def preview_url(url: str) -> dict:
    """Render a URL for the region picker. Returns the full-page screenshot so
    the user draws the rectangle in PAGE coordinates (stable across polls)."""
    url = _norm_watch_url(url)
    if not url:
        return {"ok": False, "error": "Give me a URL to load."}
    SHOTS.mkdir(parents=True, exist_ok=True)
    dest = str(SHOTS / "url_preview.png")
    if await _headless_shot(url, dest, fresh_login=True) is None:
        return {"ok": False, "error":
                "Couldn't render that page — check the URL, or close the sign-in "
                "window if one is open (the watch profile can't be shared)."}
    try:
        img_b64 = base64.standard_b64encode(Path(dest).read_bytes()).decode()
    except Exception as e:
        return {"ok": False, "error": str(e)[:120]}
    return {"ok": True, "image_b64": img_b64, "media_type": "image/png",
            "viewport": {"w": VIEW_W, "h": VIEW_H}, "url": url}


async def snapshot_url(url: str) -> tuple[bool, str]:
    """Ad-hoc headless page screenshot (agent `web_snapshot` tool). Renders the
    URL in the cookie-borrowed watch profile — works with the user's logged-in
    sessions and needs NO unlocked GUI session (usable from Slack, remotely).
    Returns (ok, png_path | error text)."""
    url = _norm_watch_url(url)
    if not url:
        return False, "Give me a URL to snapshot."
    dest_dir = SHOTS / "adhoc"
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"snap_{datetime.now():%Y%m%d_%H%M%S}.png"
    if await _headless_shot(url, str(dest)) is None:
        return False, ("Couldn't render that page headlessly — check the URL "
                       "(VPN-only sites need the VPN up), or close any open "
                       "watch-profile sign-in window.")
    return True, str(dest)


async def _web_snapshot_tool(args, ctx) -> str:
    ok, out = await snapshot_url((args or {}).get("url") or "")
    if not ok:
        return f"Error: {out}"
    return (f"Page screenshot saved to {out}. If it shows a login page, the "
            "site needs a one-time sign-in via the Vision tab preview — tell "
            "the user instead of retrying.")


async def open_signin(url: str = "") -> dict:
    """Open a HEADFUL window of the watch profile so the user can log into the
    dashboards they want to watch — cookies persist for the headless polls."""
    try:
        os.makedirs(WATCH_PROFILE, exist_ok=True)
        args = [CHROME_BIN, f"--user-data-dir={WATCH_PROFILE}", "--no-first-run",
                "--no-default-browser-check"]
        u = _norm_watch_url(url)
        if u:
            args.append(u)
        subprocess.Popen(args, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)
        return {"ok": True, "message":
                "Sign-in window opened. Log into the site, then CLOSE that window "
                "so background polling can use the profile."}
    except Exception as e:
        return {"ok": False, "message": f"Couldn't open the sign-in window: {str(e)[:100]}"}


# ─── Interactive preview session ──────────────────────────────────────────────
# A LIVE headless Chrome on the watch profile, remote-controlled from the Vision
# tab over CDP: the UI streams screenshots and forwards clicks/keys/scroll, so
# the user can navigate anywhere — including THROUGH a login screen. Everything
# done here persists in the watch profile, so a login performed in the preview
# authenticates all future polls. The session holds the headless lock (the
# profile is single-instance); an idle watchdog closes it so polls never starve.

SESSION_PORT = 9223
SESSION_IDLE_S = 240

_sess: dict = {"proc": None, "ws": None, "id": 0, "pending": {}, "reader": None,
               "frame": "", "frame_seq": 0, "last_used": 0.0, "url": ""}


def session_active() -> bool:
    return _sess["ws"] is not None


def touch_session() -> None:
    """Keep the idle watchdog at bay while a client is actively viewing."""
    _sess["last_used"] = time.time()


def session_frame_state() -> tuple[int, str, str]:
    """(frame_seq, jpeg_b64, current_url) — read by the streaming WS endpoint."""
    return _sess["frame_seq"], _sess["frame"], _sess["url"]


async def _sess_reader(ws):
    """Single reader: routes command replies to futures and captures screencast
    frames as they arrive (this is what makes the preview fast — Chrome PUSHES
    a frame on every repaint instead of us polling captureScreenshot)."""
    try:
        async for raw in ws:
            m = json.loads(raw)
            mid = m.get("id")
            if mid is not None:
                fut = _sess["pending"].pop(mid, None)
                if fut and not fut.done():
                    fut.set_result(m)
                continue
            method = m.get("method")
            p = m.get("params", {})
            if method == "Page.screencastFrame":
                _sess["frame"] = p.get("data", "")
                _sess["frame_seq"] += 1
                sid = p.get("sessionId")
                if sid is not None:
                    # Ack immediately or Chrome stops sending frames.
                    asyncio.create_task(
                        _sess_cmd("Page.screencastFrameAck", {"sessionId": sid},
                                  timeout=5, quiet=True))
            elif method == "Page.frameNavigated":
                fr = p.get("frame", {})
                if not fr.get("parentId"):
                    _sess["url"] = fr.get("url", "") or _sess["url"]
    except Exception:
        pass


async def _sess_cmd(method: str, params: dict | None = None, timeout: float = 15,
                    quiet: bool = False):
    """One CDP command; the reply is routed back by the reader task."""
    ws = _sess["ws"]
    if ws is None:
        raise RuntimeError("no session")
    _sess["id"] += 1
    mid = _sess["id"]
    fut = asyncio.get_event_loop().create_future()
    _sess["pending"][mid] = fut
    try:
        await ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
        m = await asyncio.wait_for(fut, timeout)
    except Exception:
        _sess["pending"].pop(mid, None)
        if quiet:
            return {}
        raise
    if m.get("error"):
        if quiet:
            return {}
        raise RuntimeError(str(m["error"])[:120])
    return m.get("result", {})


async def start_session(url: str) -> dict:
    """Launch (or reuse) the interactive preview browser and navigate to url."""
    try:
        import httpx
        import websockets
    except Exception:
        return {"ok": False, "error": "httpx/websockets not installed."}
    url = _norm_watch_url(url)
    if not url:
        return {"ok": False, "error": "Give me a URL to load."}
    if session_active():
        try:
            await _sess_cmd("Page.navigate", {"url": url})
            _sess["last_used"] = time.time()
            _sess["url"] = url
            return {"ok": True, "viewport": {"w": VIEW_W, "h": VIEW_H}, "reused": True}
        except Exception:
            await stop_session()
    await _HEADLESS_LOCK.acquire()       # released by stop_session()
    try:
        await asyncio.to_thread(_reap_profile_orphans)
        await asyncio.to_thread(_sync_profile_state, True)
        os.makedirs(WATCH_PROFILE, exist_ok=True)
        proc = await asyncio.create_subprocess_exec(
            CHROME_BIN, "--headless=new", "--disable-gpu", "--hide-scrollbars",
            f"--remote-debugging-port={SESSION_PORT}",
            f"--user-data-dir={WATCH_PROFILE}",
            f"--window-size={VIEW_W},{VIEW_H}",
            "--no-first-run", "--no-default-browser-check", url,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
            start_new_session=True)
        _sess["proc"] = proc
        # Wait for the debug port + a page target.
        ws_url = None
        async with httpx.AsyncClient(timeout=2) as c:
            for _ in range(40):
                await asyncio.sleep(0.5)
                try:
                    targets = (await c.get(f"http://localhost:{SESSION_PORT}/json/list")).json()
                    pages = [t for t in targets if t.get("type") == "page"
                             and t.get("webSocketDebuggerUrl")]
                    if pages:
                        ws_url = pages[0]["webSocketDebuggerUrl"]
                        break
                except Exception:
                    continue
        if not ws_url:
            raise RuntimeError("debug port never came up")
        _sess["ws"] = await websockets.connect(ws_url, max_size=None)
        _sess["pending"] = {}
        _sess["frame"] = ""
        _sess["frame_seq"] = 0
        _sess["reader"] = asyncio.create_task(_sess_reader(_sess["ws"]))
        await _sess_cmd("Page.enable")
        await _sess_cmd("Runtime.enable")
        # CRITICAL: in interactive mode Chrome reserves ~87px of the window for
        # its (headless) UI, shrinking the page viewport to 1440×813 — while
        # poll one-shots render a true 1440×900. Regions drawn on one don't map
        # to the other (the "selection is slightly off" bug). Overriding device
        # metrics pins the interactive viewport to exactly VIEW_W×VIEW_H.
        await _sess_cmd("Emulation.setDeviceMetricsOverride",
                        {"width": VIEW_W, "height": VIEW_H,
                         "deviceScaleFactor": 1, "mobile": False})
        # Screencast: Chrome pushes a scaled JPEG on every repaint. maxWidth
        # keeps frames small (~20-60KB) so the stream feels live.
        await _sess_cmd("Page.startScreencast",
                        {"format": "jpeg", "quality": 62,
                         "maxWidth": 1024, "maxHeight": 1024 * VIEW_H // VIEW_W,
                         "everyNthFrame": 1})
        _sess["last_used"] = time.time()
        _sess["url"] = url
        return {"ok": True, "viewport": {"w": VIEW_W, "h": VIEW_H}}
    except Exception as e:
        await stop_session()
        return {"ok": False, "error": f"Couldn't start the preview browser: {str(e)[:120]}"}


async def session_frame() -> dict:
    """Latest screencast frame (REST fallback — the WS stream is the fast path)."""
    if not session_active():
        return {"ok": False, "error": "No live preview session."}
    _sess["last_used"] = time.time()
    if not _sess["frame"]:
        # Screencast hasn't produced a frame yet — pull one directly.
        try:
            shot = await _sess_cmd("Page.captureScreenshot",
                                   {"format": "jpeg", "quality": 62})
            _sess["frame"] = shot.get("data", "")
        except Exception as e:
            return {"ok": False, "error": str(e)[:120]}
    return {"ok": True, "image_b64": _sess["frame"], "media_type": "image/jpeg",
            "url": _sess["url"], "viewport": {"w": VIEW_W, "h": VIEW_H}}


_VK = {"Enter": 13, "Backspace": 8, "Tab": 9, "Escape": 27, "Delete": 46,
       "ArrowLeft": 37, "ArrowUp": 38, "ArrowRight": 39, "ArrowDown": 40,
       "PageUp": 33, "PageDown": 34, "Home": 36, "End": 35}


async def session_input(action: dict) -> dict:
    """Forward one user interaction into the live preview."""
    if not session_active():
        return {"ok": False, "error": "No live preview session."}
    _sess["last_used"] = time.time()
    t = action.get("type")
    x = float(action.get("x") or 0) * VIEW_W
    y = float(action.get("y") or 0) * VIEW_H
    try:
        if t == "click":
            for typ in ("mousePressed", "mouseReleased"):
                await _sess_cmd("Input.dispatchMouseEvent", {
                    "type": typ, "x": x, "y": y, "button": "left", "clickCount": 1})
        elif t == "scroll":
            await _sess_cmd("Input.dispatchMouseEvent", {
                "type": "mouseWheel", "x": x or VIEW_W / 2, "y": y or VIEW_H / 2,
                "deltaX": 0, "deltaY": float(action.get("dy") or 0)})
        elif t == "text":
            await _sess_cmd("Input.insertText", {"text": str(action.get("text") or "")})
        elif t == "key":
            k = str(action.get("key") or "Enter")
            vk = _VK.get(k, 0)
            for typ in ("rawKeyDown", "keyUp"):
                await _sess_cmd("Input.dispatchKeyEvent", {
                    "type": typ, "key": k, "code": k,
                    "windowsVirtualKeyCode": vk, "nativeVirtualKeyCode": vk})
        elif t == "navigate":
            await _sess_cmd("Page.navigate", {"url": _norm_watch_url(action.get("url") or "")})
        elif t == "back":
            await _sess_cmd("Runtime.evaluate", {"expression": "history.back()"})
        elif t == "scrolltop":
            # Entering SELECT mode: polls render the page from the TOP, so the
            # region must be drawn against the top-of-page view.
            await _sess_cmd("Runtime.evaluate", {"expression": "window.scrollTo(0,0)"})
        else:
            return {"ok": False, "error": f"unknown input '{t}'"}
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": str(e)[:120]}


async def stop_session() -> dict:
    """Close the interactive browser GRACEFULLY — SIGTERM lets Chrome flush the
    session (cookies etc.) to the profile, which is what makes a login done in
    the preview stick for future polls. SIGKILL only as a last resort."""
    import signal
    ws, proc = _sess["ws"], _sess["proc"]
    _sess["ws"] = None
    _sess["proc"] = None
    if _sess["reader"] is not None:
        _sess["reader"].cancel()
        _sess["reader"] = None
    _sess["pending"] = {}
    _sess["frame"] = ""
    _sess["frame_seq"] = 0
    if ws is not None:
        try:
            await ws.close()
        except Exception:
            pass
    if proc is not None:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except Exception:
            pass
        try:
            await asyncio.wait_for(proc.wait(), timeout=8)
        except Exception:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except Exception:
                pass
    if _HEADLESS_LOCK.locked():
        _HEADLESS_LOCK.release()
    return {"ok": True}


async def _thumb_hash(png: str) -> str:
    """Hash of a tiny (64px) JPEG of the capture — the cheap did-anything-move
    gate. sips encoding is deterministic for identical pixels."""
    tiny = png.rsplit(".", 1)[0] + ".tiny.jpg"
    try:
        proc = await asyncio.create_subprocess_exec(
            "sips", "-s", "format", "jpeg", "-s", "formatOptions", "60",
            "--resampleWidth", "64", png, "--out", tiny,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        await asyncio.wait_for(proc.communicate(), timeout=10)
        return hashlib.sha1(Path(tiny).read_bytes()).hexdigest()
    except Exception:
        try:
            return hashlib.sha1(Path(png).read_bytes()).hexdigest()
        except Exception:
            return ""


# ─── the check cycle ──────────────────────────────────────────────────────────

async def _read_value(w: dict, png: str) -> str:
    """Vision call: read the watched value/state from the region capture."""
    from services import agent
    path, media_type = await vision._downscale_for_vision(png)
    img_b64 = vision._encode_image(path)
    prompt = (f"This is a cropped region of the user's screen. They are watching: "
              f"\"{w.get('question') or 'this region'}\".\n"
              "Reply with ONLY the current value/state as one short line "
              "(exact numbers/text as displayed, e.g. '1,284' or 'Status: FAILED, red'). "
              "No commentary.")
    resp = await llm.acreate(
        model=agent.AGENT_MODEL, fallbacks=llm.FAST_FALLBACKS, max_tokens=100,
        messages=[{"role": "user", "content": [
            {"type": "image", "source": {"type": "base64",
                                         "media_type": media_type, "data": img_b64}},
            {"type": "text", "text": prompt}]}])
    return llm.extract_text(resp).strip()[:200]


async def _judge_condition(w: dict, prev: str, cur: str) -> dict:
    """Natural-language condition → {alert: bool, reason: str} (text LLM)."""
    from services import agent
    prompt = (f"A screen watcher tracks: \"{w.get('question')}\".\n"
              f"Alert condition: \"{w.get('condition')}\".\n"
              f"Previous reading: \"{prev or '(none yet)'}\"\n"
              f"Current reading: \"{cur}\"\n\n"
              "Respond with ONLY one JSON object, exactly these keys: "
              '{"alert": true|false, "reason": "one short line"}')
    try:
        resp = await llm.acreate(model=agent.AGENT_MODEL,
                                 fallbacks=llm.FAST_FALLBACKS, max_tokens=120,
                                 messages=[{"role": "user", "content": prompt}])
        raw = llm.extract_text(resp)
        import re
        m = re.search(r"\{.*\}", raw, re.S)
        if m:
            j = json.loads(m.group(0))
            return {"alert": bool(j.get("alert")),
                    "reason": str(j.get("reason", ""))[:160]}
    except Exception as e:
        print(f"[watchers] condition judge failed: {e}")
    return {"alert": False, "reason": ""}


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


async def _notify_macos(title: str, text: str) -> None:
    try:
        esc_t = title.replace('"', "'")[:60]
        esc_x = text.replace('"', "'")[:180]
        proc = await asyncio.create_subprocess_exec(
            "osascript", "-e",
            f'display notification "{esc_x}" with title "{esc_t}" sound name "Ping"',
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        await proc.communicate()
    except Exception:
        pass


async def check(wid: str, manual: bool = False) -> dict:
    """One full check cycle for a watcher. Returns what happened."""
    w = get(wid)
    if not w:
        return {"ok": False, "message": "No such watcher."}
    lock = _check_locks.setdefault(wid, asyncio.Lock())
    if lock.locked():
        return {"ok": False, "message": "Already checking."}
    async with lock:
        SHOTS.mkdir(parents=True, exist_ok=True)
        png = str(SHOTS / f"{wid}.png")
        if w.get("type") == "url":
            captured = await _capture_url(w, png)
            fail_msg = ("page render failed — bad URL, or the sign-in window is "
                        "open (close it; the profile can't be shared)")
            if captured is not None and _LAST_SHOT_LOGIN_WALL:
                # Session expired — the page is a sign-in wall; reading the
                # region would produce garbage. Tell the user how to fix it.
                msg = ("signed out — open this URL in the Vision live preview "
                       "and sign in once")
                _update(wid, last_error=msg, last_checked=_now_iso())
                return {"ok": False, "message": f"Watcher “{w['name']}”: {msg}."}
        else:
            captured = await _capture_region(w, png)
            fail_msg = "screenshot failed (Screen Recording permission?)"
        if captured is None:
            _update(wid, last_error=fail_msg, last_checked=_now_iso())
            return {"ok": False, "message": "Capture failed."}

        new_hash = await _thumb_hash(png)
        prev_hash = w.get("last_hash", "")
        if not manual and new_hash and new_hash == prev_hash:
            # Nothing moved — skip the LLM entirely.
            _update(wid, last_checked=_now_iso(), last_error="")
            return {"ok": True, "changed": False, "skipped": True,
                    "value": w.get("last_value", "")}

        try:
            value = await _read_value(w, png)
        except Exception as e:
            _update(wid, last_error=f"vision read failed: {str(e)[:80]}",
                    last_checked=_now_iso())
            return {"ok": False, "message": "Vision read failed."}

        prev_value = w.get("last_value", "")
        value_changed = bool(prev_value) and value.strip().lower() != prev_value.strip().lower()
        first_read = not prev_value

        # Alert decision
        alert, reason = False, ""
        if w.get("condition"):
            if value_changed or first_read:
                j = await _judge_condition(w, prev_value, value)
                alert, reason = j["alert"], j["reason"]
        else:
            alert = value_changed
            reason = f'Changed from "{prev_value}" to "{value}"' if value_changed else ""

        # Cooldown: same alert value within the window doesn't re-fire.
        now = time.time()
        if alert and w.get("_last_alert_value") == value and \
                now - float(w.get("_last_alert_ts") or 0) < ALERT_COOLDOWN_S:
            alert = False

        hist = (w.get("history") or [])
        hist.append({"ts": _now_iso(), "value": value, "alert": alert,
                     "reason": reason if alert else ""})
        fields = {"last_checked": _now_iso(), "last_value": value,
                  "last_hash": new_hash, "last_error": "",
                  "history": hist[-HISTORY_CAP:]}
        if alert:
            fields["_last_alert_value"] = value
            fields["_last_alert_ts"] = now
            fields["alerts_fired"] = int(w.get("alerts_fired") or 0) + 1
        _update(wid, **fields)

        if alert:
            title = f"👁 {w.get('name') or 'Watcher'}"
            line = reason or f"Now: {value}"
            print(f"[watchers] ALERT {w.get('name')}: {line}")
            if _broadcast:
                try:
                    await _broadcast({"type": "speak",
                                      "text": f"Sir — {w.get('name')}: {line}"})
                    await _broadcast({"type": "briefing_card", "title": title,
                                      "markdown": (f"**{w.get('question','')}**\n\n"
                                                   f"{line}\n\n"
                                                   f"- Previous: `{prev_value or '—'}`\n"
                                                   f"- Current: `{value}`")})
                except Exception:
                    pass
            await _notify_macos(title, line)
            # REFLEX — the watcher acts, not just alerts. Fire-and-forget so a
            # long macro/agent run never blocks this or other checks.
            if (w.get("reflex") or {}).get("kind") in ("macro", "prompt"):
                asyncio.create_task(_fire_reflex(wid, value, prev_value, reason))

        return {"ok": True, "changed": value_changed, "alert": alert,
                "value": value, "reason": reason}


# ─── Reflex: watcher-triggered actions ────────────────────────────────────────
# A watcher can carry a reflex — an action that fires WITH its alert:
#   kind="macro"  → replay a Kinesis macro (GUI hands)
#   kind="prompt" → run an agent task headlessly ({value}/{previous}/{reason}/
#                   {name}/{url} placeholders are substituted from the reading)
# Reflexes respect the global run lock (never fight a live user run or another
# job for the machine), have their own cooldown, and report outcome to the HUD.

REFLEX_DEFAULT_COOLDOWN_S = 600
REFLEX_LOCK_WAIT_S = 180          # give a busy agent run this long, then skip
_reflex_running: set[str] = set()


def _sanitize_reflex(raw: dict | None) -> dict:
    r = raw or {}
    kind = r.get("kind") if r.get("kind") in ("macro", "prompt") else "none"
    return {
        "kind": kind,
        "macro": (r.get("macro") or "").strip()[:80] if kind == "macro" else "",
        "prompt": (r.get("prompt") or "").strip()[:1000] if kind == "prompt" else "",
        "cooldown_s": max(60, int(r.get("cooldown_s") or REFLEX_DEFAULT_COOLDOWN_S)),
        "last_fired": r.get("last_fired") or "",
        "last_result": (r.get("last_result") or "")[:200],
        "fires": int(r.get("fires") or 0),
    }


def _set_reflex_result(wid: str, rx: dict, result: str, fired: bool) -> None:
    w = get(wid)
    if not w:
        return
    cur = _sanitize_reflex(w.get("reflex"))
    if fired:
        cur["last_fired"] = _now_iso()
        cur["fires"] += 1
    cur["last_result"] = result[:200]
    _update(wid, reflex=cur)


async def _fire_reflex(wid: str, value: str, prev_value: str, reason: str) -> None:
    w = get(wid)
    if not w:
        return
    rx = _sanitize_reflex(w.get("reflex"))
    if rx["kind"] not in ("macro", "prompt"):
        return
    # Per-watcher reflex cooldown (independent of the alert cooldown).
    if rx["last_fired"]:
        try:
            last = datetime.fromisoformat(rx["last_fired"]).timestamp()
            if time.time() - last < rx["cooldown_s"]:
                print(f"[watchers] reflex {w.get('name')}: in cooldown — skipped")
                return
        except Exception:
            pass
    if wid in _reflex_running:
        return
    _reflex_running.add(wid)
    outcome, ok = "", False
    try:
        if rx["kind"] == "macro":
            outcome, ok = await _reflex_macro(w, rx)
        else:
            outcome, ok = await _reflex_prompt(w, rx, value, prev_value, reason)
    except Exception as e:
        outcome = f"reflex error: {str(e)[:120]}"
    finally:
        _reflex_running.discard(wid)
    _set_reflex_result(wid, rx, outcome, fired=ok)
    print(f"[watchers] reflex {w.get('name')}: {outcome[:120]}")
    if _broadcast:
        try:
            await _broadcast({"type": "briefing_card",
                              "title": f"⚡ Reflex · {w.get('name')}",
                              "markdown": (f"**Trigger:** {reason or value}\n\n"
                                           f"**Action:** {rx['kind']} "
                                           f"{'`' + rx['macro'] + '`' if rx['kind'] == 'macro' else ''}\n\n"
                                           f"**Outcome:** {outcome[:400]}")})
        except Exception:
            pass


async def _acquire_run_lock() -> bool:
    if _run_lock is None:
        return True
    try:
        await asyncio.wait_for(_run_lock.acquire(), timeout=REFLEX_LOCK_WAIT_S)
        return True
    except Exception:
        return False


def _release_run_lock() -> None:
    if _run_lock is not None and _run_lock.locked():
        try:
            _run_lock.release()
        except Exception:
            pass


async def _reflex_macro(w: dict, rx: dict) -> tuple[str, bool]:
    from services import kinesis
    macro = kinesis.get_macro(rx["macro"])
    if not macro:
        return f"macro '{rx['macro']}' not found", False
    if not await _acquire_run_lock():
        return "skipped — the agent was busy for too long", False
    try:
        res = await kinesis.replay(macro)
        return res.get("message", "replayed"), bool(res.get("ok"))
    finally:
        _release_run_lock()


def _sub_placeholders(prompt: str, w: dict, value: str, prev: str, reason: str) -> str:
    subs = {"{value}": value, "{previous}": prev or "—", "{reason}": reason or "",
            "{name}": w.get("name", ""), "{url}": w.get("url", ""),
            "{question}": w.get("question", "")}
    for k, v in subs.items():
        prompt = prompt.replace(k, v)
    return prompt


async def _reflex_prompt(w: dict, rx: dict, value: str, prev: str,
                         reason: str) -> tuple[str, bool]:
    from services import agent
    from services.scheduler import _HeadlessInteraction
    prompt = _sub_placeholders(rx["prompt"], w, value, prev, reason)
    # Context header so the agent knows WHY it's running.
    prompt = (f"[Reflex from watcher \"{w.get('name')}\" — it read \"{value}\" "
              f"(was \"{prev or '—'}\"){' — ' + reason if reason else ''}]\n{prompt}")
    if not await _acquire_run_lock():
        return "skipped — the agent was busy for too long", False
    try:
        async def emit(event: dict) -> None:
            pass
        final = await agent.run_task(prompt, emit, _HeadlessInteraction(),
                                     history=[], mode="full", unattended=True)
        return (final or "done").strip()[:300], True
    finally:
        _release_run_lock()


# ─── CRUD ─────────────────────────────────────────────────────────────────────

def create(name: str, question: str, region: dict, interval_s: int = 60,
           condition: str = "", wtype: str = "screen", url: str = "",
           viewport: dict | None = None, reflex: dict | None = None) -> dict:
    name = (name or "").strip() or "Watcher"
    question = (question or "").strip()
    if not question:
        return {"ok": False, "message": "Say what to watch (e.g. 'the settlement count')."}
    wtype = "url" if wtype == "url" else "screen"
    url = _norm_watch_url(url) if wtype == "url" else ""
    if wtype == "url" and not url:
        return {"ok": False, "message": "URL watchers need a URL."}
    r = {k: float(region.get(k, 0)) for k in ("x", "y", "w", "h")}
    if r["w"] < 0.01 or r["h"] < 0.01:
        return {"ok": False, "message": "Region too small — drag a bigger rectangle."}
    wid = f"w{int(time.time()*1000):x}"
    items = _load()
    items.append({
        "id": wid, "name": name[:60], "question": question[:200],
        "condition": (condition or "").strip()[:200],
        "type": wtype, "url": url,
        "viewport": {"w": int((viewport or {}).get("w") or VIEW_W),
                     "h": int((viewport or {}).get("h") or VIEW_H)},
        "region": r, "interval_s": max(MIN_INTERVAL_S, int(interval_s or 60)),
        "enabled": True, "created": _now_iso(),
        "reflex": _sanitize_reflex(reflex),
        "last_checked": "", "last_value": "", "last_hash": "", "last_error": "",
        "alerts_fired": 0, "history": [],
    })
    _save(items)
    return {"ok": True, "id": wid, "message": f"Watching “{name}”."}


def update_watcher(wid: str, body: dict | None) -> dict:
    """Edit a watcher's metadata: name, question, condition, interval, URL.
    (Region changes need the picker — delete & recreate for those.)"""
    w = get(wid)
    if not w:
        return {"ok": False, "message": "No such watcher."}
    b = body or {}
    fields: dict = {}
    if (b.get("name") or "").strip():
        fields["name"] = b["name"].strip()[:60]
    if (b.get("question") or "").strip():
        q = b["question"].strip()[:200]
        if q != w.get("question"):
            fields["question"] = q
            fields["last_hash"] = ""      # question changed — force a fresh read
    if "condition" in b:
        c = (b.get("condition") or "").strip()[:200]
        if c != w.get("condition"):
            fields["condition"] = c
            # Alert semantics changed — drop the alert-dedup state so the new
            # condition is judged cleanly on the next change.
            fields["_last_alert_value"] = ""
            fields["_last_alert_ts"] = 0
    if b.get("interval_s"):
        fields["interval_s"] = max(MIN_INTERVAL_S, int(b["interval_s"]))
    if (b.get("url") or "").strip() and w.get("type") == "url":
        u = _norm_watch_url(b["url"])
        if u != w.get("url"):
            fields["url"] = u
            fields["last_hash"] = ""      # new page — re-read
    if not fields:
        return {"ok": True, "message": "Nothing changed."}
    _update(wid, **fields)
    return {"ok": True, "message": f"Watcher “{fields.get('name', w.get('name'))}” updated."}


def set_reflex(wid: str, raw: dict | None) -> dict:
    """Attach / update / clear a watcher's reflex (kind=none clears)."""
    w = get(wid)
    if not w:
        return {"ok": False, "message": "No such watcher."}
    old = _sanitize_reflex(w.get("reflex"))
    new = _sanitize_reflex(raw)
    # Preserve fire history across edits.
    new["last_fired"], new["last_result"], new["fires"] = \
        old["last_fired"], old["last_result"], old["fires"]
    if new["kind"] == "macro" and not new["macro"]:
        return {"ok": False, "message": "Pick a macro for the reflex."}
    if new["kind"] == "prompt" and not new["prompt"]:
        return {"ok": False, "message": "Write the agent prompt for the reflex."}
    _update(wid, reflex=new)
    label = {"none": "cleared", "macro": f"runs macro “{new['macro']}”",
             "prompt": "runs an agent task"}[new["kind"]]
    return {"ok": True, "message": f"Reflex {label}.", "reflex": new}


def delete(wid: str) -> dict:
    items = _load()
    keep = [w for w in items if w.get("id") != wid]
    if len(keep) == len(items):
        return {"ok": False, "message": "No such watcher."}
    _save(keep)
    for suffix in (".png", ".tiny.jpg", ".jpg", ".full.png"):
        try:
            os.remove(SHOTS / f"{wid}{suffix}")
        except OSError:
            pass
    return {"ok": True, "message": "Watcher removed."}


def toggle(wid: str) -> dict:
    w = get(wid)
    if not w:
        return {"ok": False, "message": "No such watcher."}
    _update(wid, enabled=not w.get("enabled", True))
    return {"ok": True, "enabled": not w.get("enabled", True)}


def thumb_path(wid: str) -> str | None:
    p = SHOTS / f"{wid}.png"
    return str(p) if p.exists() else None


# ─── tick loop ────────────────────────────────────────────────────────────────

async def _tick_loop():
    while True:
        try:
            await asyncio.sleep(5)
            now = time.time()
            # Idle preview session → close it (it holds the profile lock; an
            # abandoned one would starve every URL watcher).
            if session_active() and now - _sess["last_used"] > SESSION_IDLE_S:
                print("[watchers] closing idle preview session")
                await stop_session()
            # While Kinesis is recording, a headless capture Chrome would steal
            # AppleScript targeting from the user's real browser and corrupt the
            # recording — hold URL polls until it finishes (screen watchers are
            # screencapture-based and unaffected).
            kinesis_rec = False
            try:
                from services import kinesis as _kin
                kinesis_rec = bool(_kin.recording_status().get("recording"))
            except Exception:
                pass
            for w in _load():
                if not w.get("enabled", True):
                    continue
                if kinesis_rec and w.get("type") == "url":
                    continue                     # retried next tick after recording
                due_at = float(w.get("_next_due") or 0)
                if now < due_at:
                    continue
                _update(w["id"], _next_due=now + max(MIN_INTERVAL_S,
                                                     int(w.get("interval_s") or 60)))
                # Fire-and-forget so one slow vision call never starves the rest.
                asyncio.create_task(check(w["id"]))
        except asyncio.CancelledError:
            return
        except Exception as e:
            print(f"[watchers] tick error: {e}")


# ─── agent tools ──────────────────────────────────────────────────────────────

def _find_watcher(name: str) -> dict | None:
    q = (name or "").strip().lower()
    if not q:
        return None
    items = _load()
    return (next((w for w in items if (w.get("name", "") or "").lower() == q), None)
            or next((w for w in items if q in (w.get("name", "") or "").lower()), None))


def _watcher_line(w: dict) -> str:
    status = ("ERROR: " + w["last_error"] if w.get("last_error")
              else "watching" if w.get("enabled", True) else "paused")
    rx = w.get("reflex") or {}
    reflex = (f" · reflex={rx.get('kind')}" +
              (f"({rx.get('macro')})" if rx.get("kind") == "macro" else "")
              if rx.get("kind") in ("macro", "prompt") else "")
    return (f"- {w.get('name')}: \"{w.get('question')}\" → {w.get('last_value') or '—'} "
            f"[{status}] every {int(w.get('interval_s') or 60) // 60 or 1}m"
            f"{' · alert when ' + w['condition'] if w.get('condition') else ''}{reflex}")


async def _watchers_tool(args, ctx) -> str:
    action = ((args or {}).get("action") or "list").lower()
    items = _load()
    if not items:
        return ("No watchers exist yet. They're created visually (draw a region on a "
                "page) — send the user to the Vision tab to make one.")
    if action == "list":
        return "Vision watchers:\n" + "\n".join(_watcher_line(w) for w in items)
    w = _find_watcher((args or {}).get("name") or "")
    if not w:
        return ("No watcher named that. Existing: "
                + ", ".join(x.get("name", "?") for x in items))
    if action == "check":
        res = await check(w["id"], manual=True)
        if not res.get("ok"):
            return f"Error: check failed — {res.get('message', '?')}"
        return (f"{w['name']} reads: {res.get('value')}"
                + (f" — ALERT: {res.get('reason')}" if res.get("alert") else " (no alert)"))
    if action == "history":
        rows = (get(w["id"]) or {}).get("history", [])[-10:]
        return (f"History for {w['name']}:\n"
                + "\n".join(f"- {h.get('ts','')[11:19]} {h.get('value','')}"
                            + (" ⚡" + h.get("reason", "") if h.get("alert") else "")
                            for h in rows)) if rows else "No reads yet."
    return "Error: unknown action — use list | check | history."


async def _watcher_set_tool(args, ctx) -> str:
    a = args or {}
    action = (a.get("action") or "").lower()
    w = _find_watcher(a.get("name") or "")
    if not w:
        return ("No watcher named that. Existing: "
                + ", ".join(x.get("name", "?") for x in _load()) or "none")
    wid = w["id"]
    if action in ("pause", "resume"):
        cur = w.get("enabled", True)
        want = action == "resume"
        if cur == want:
            return f"{w['name']} is already {'watching' if cur else 'paused'}."
        toggle(wid)
        return f"{w['name']} {'resumed' if want else 'paused'}."
    if action == "reflex":
        kind = (a.get("kind") or "").lower()
        if kind not in ("none", "macro", "prompt"):
            return "Error: reflex kind must be none | macro | prompt."
        res = set_reflex(wid, {"kind": kind, "macro": a.get("macro") or "",
                               "prompt": a.get("prompt") or "",
                               "cooldown_s": int(a.get("cooldown_s") or 600)})
        return res.get("message", "done") if res.get("ok") else f"Error: {res.get('message')}"
    if action == "interval":
        res = update_watcher(wid, {"interval_s": int(a.get("interval_s") or 60)})
        return res.get("message", "done")
    return "Error: unknown action — use pause | resume | reflex | interval."


def register_agent_tools() -> None:
    """Register `watchers` (read) + `watcher_set` (control) so the agent can
    answer 'what are you watching?' and arm reflexes from chat/voice."""
    try:
        from services import agent
        agent.register_tool({
            "name": "watchers",
            "description": (
                "The user's Vision watchers — screen/page regions COSMOS polls and reads "
                "with vision (dashboard numbers, statuses). actions: list (default, with "
                "current values) | check (re-read one NOW; name required) | history "
                "(recent readings; name required). Watchers are CREATED visually in the "
                "Vision tab — never try to create one here."),
            "input_schema": {"type": "object", "properties": {
                "action": {"type": "string", "enum": ["list", "check", "history"]},
                "name": {"type": "string", "description": "watcher name (fuzzy ok)"},
            }},
        }, _watchers_tool, gate="open", label="watchers", source="watchers")
        agent.register_tool({
            "name": "watcher_set",
            "description": (
                "Control a Vision watcher: pause/resume polling, change its interval, or "
                "arm a REFLEX — an action that fires automatically when the watcher "
                "alerts (kind=macro replays a Kinesis macro by name; kind=prompt runs an "
                "agent task — placeholders {value} {previous} {reason} {name} {url}; "
                "kind=none clears). actions: pause | resume | reflex | interval."),
            "input_schema": {"type": "object", "properties": {
                "action": {"type": "string", "enum": ["pause", "resume", "reflex", "interval"]},
                "name": {"type": "string", "description": "watcher name (fuzzy ok)"},
                "kind": {"type": "string", "enum": ["none", "macro", "prompt"]},
                "macro": {"type": "string"}, "prompt": {"type": "string"},
                "cooldown_s": {"type": "integer"}, "interval_s": {"type": "integer"},
            }, "required": ["action", "name"]},
        }, _watcher_set_tool, gate="confirm", label="watcher control", source="watchers")
        agent.register_tool({
            "name": "web_snapshot",
            "description": (
                "Screenshot a WEB PAGE headlessly (no visible browser, works even "
                "with the screen locked or the user away). Renders the URL with the "
                "user's logged-in Chrome sessions and returns the saved PNG path — "
                "pass it to slack_deliver / slack_photo / write_file. PREFER this "
                "over open_url+screenshot for 'screenshot <dashboard/page>' asks. "
                "If the result is a login page, the site needs a one-time sign-in "
                "via the Vision tab — report that instead of retrying."),
            "input_schema": {"type": "object", "properties": {
                "url": {"type": "string", "description": "page URL (https:// assumed)"},
            }, "required": ["url"]},
        }, _web_snapshot_tool, gate="open", timeout=120.0, artifact=True,
            label="page snapshot", source="watchers")
        agent.invalidate_tool_cache()
    except Exception as e:
        print(f"[watchers] agent tool registration failed (non-fatal): {e}")


def start(broadcast, run_lock: asyncio.Lock | None = None) -> None:
    """Start the poll loop (idempotent). Mirrors scheduler.start(). `run_lock`
    is main's global run lock — reflex actions serialize behind it so they
    never fight a live user run (or each other) for the machine."""
    global _task, _broadcast, _run_lock
    _broadcast = broadcast
    _run_lock = run_lock
    if _task and not _task.done():
        return
    _task = asyncio.get_event_loop().create_task(_tick_loop())
    print("[watchers] online — eyes open")
