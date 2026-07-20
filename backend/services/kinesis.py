"""Kinesis — demonstrate-once macro recorder/replayer (semantic capture).

"Watch me do this once." Kinesis records a GUI chore and compiles it into a
named, replayable macro — but it captures the *meaning* of each action, not
just pixels:

- WEB (Chrome/Chromium): an in-page DOM recorder injected via `execute
  javascript` captures, per action, a robust CSS selector + the element's
  visible text, role and label, plus form input values, scroll position and
  navigation. On replay it finds the element by identity (selector → text →
  role) and dispatches a real click — robust to moved windows, scroll and
  re-renders. If it can't be found, an LLM picks the best match from the live
  page's elements (the "agent fallback").
- NATIVE apps: the Quartz event tap captures clicks/keys, and each click is
  enriched with the Accessibility element under the cursor (role + title) via
  AXUIElementCopyElementAtPosition. Replay clicks by element name/role, with a
  coordinate fallback.

Because replay drives the user's real, signed-in Chrome, saved sessions and
autofill carry sign-in flows through — Kinesis never handles raw credentials.

The two capture domains are disjoint (the tap skips clicks/keys while a browser
is frontmost; the DOM poller owns those), then merged on a wall-clock timeline.
Everything is best-effort and guarded: a recorder failure never takes down the
backend.
"""

import os
import re
import json
import time
import queue
import asyncio
import threading
import subprocess
from pathlib import Path
from datetime import datetime

from services import atomicio

try:
    import Quartz  # pyobjc
    _HAS_QUARTZ = True
except Exception:  # pragma: no cover - macOS only
    _HAS_QUARTZ = False

try:
    import ApplicationServices as _AXS      # Accessibility C-API
    _HAS_AX = True
except Exception:
    _HAS_AX = False

MACROS_DIR = Path.home() / ".friday" / "macros"
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,40}$")
_MAX_STEPS = 1200
_REPLAY_DELAY_CAP_MS = 2500

_SPECIAL_KEYCODES = {
    36: "return", 76: "enter", 48: "tab", 49: "space", 51: "delete",
    53: "escape", 117: "forward_delete", 123: "left", 124: "right",
    125: "down", 126: "up", 115: "home", 119: "end", 116: "pageup",
    121: "pagedown", 122: "f1", 120: "f2", 99: "f3", 118: "f4",
}
_NAME_TO_KEYCODE = {v: k for k, v in _SPECIAL_KEYCODES.items()}

# Common browser shortcuts → a friendly step label. Keyed by (sorted-mods, keycode).
_BROWSER_CHORD_NAMES = {
    (("command",), 49): "Spotlight search (⌘Space)",
    (("command",), 17): "Open new tab (⌘T)",
    (("command",), 13): "Close tab (⌘W)",
    (("command",), 37): "Focus address bar (⌘L)",
    (("command",), 15): "Reload (⌘R)",
    (("command",), 45): "New window (⌘N)",
    (("command", "shift"), 17): "Reopen closed tab (⇧⌘T)",
    (("command", "shift"), 45): "New incognito window (⇧⌘N)",
}

# Frontmost-app localizedName → (AppleScript app name, engine).
_BROWSERS = {
    "Google Chrome": ("Google Chrome", "chrome"),
    "Google Chrome Beta": ("Google Chrome Beta", "chrome"),
    "Google Chrome Canary": ("Google Chrome Canary", "chrome"),
    "Google Chrome Dev": ("Google Chrome Dev", "chrome"),
    "Chromium": ("Chromium", "chrome"),
    "Brave Browser": ("Brave Browser", "chrome"),
    "Microsoft Edge": ("Microsoft Edge", "chrome"),
    "Arc": ("Arc", "chrome"),
    "Vivaldi": ("Vivaldi", "chrome"),
    "Opera": ("Opera", "chrome"),
    "Safari": ("Safari", "safari"),
    "Safari Technology Preview": ("Safari Technology Preview", "safari"),
}

# AXRole → System Events element type (for native click-by-name replay).
_AXROLE_TO_SE = {
    "AXButton": "button", "AXMenuItem": "menu item", "AXMenuButton": "menu button",
    "AXTextField": "text field", "AXTextArea": "text area", "AXCheckBox": "checkbox",
    "AXRadioButton": "radio button", "AXPopUpButton": "pop up button",
    "AXStaticText": "static text", "AXImage": "image", "AXLink": "button",
    "AXTab": "tab button", "AXRow": "row", "AXCell": "cell",
}

# Never capture the Cosmos HUD's own UI as macro steps.
_FRIDAY_ORIGINS = ("localhost:5173", "127.0.0.1:5173", "localhost:8000", "127.0.0.1:8000")


def _browser_of(app: str | None):
    return _BROWSERS.get(app or "")


def _is_friday_url(u: str | None) -> bool:
    u = (u or "").lower()
    return any(o in u for o in _FRIDAY_ORIGINS)


def _norm_url(u: str | None) -> str:
    if not u:
        return ""
    return u.split("#", 1)[0].rstrip("/")


def _trim(s, n: int = 48) -> str:
    s = (s or "").strip()
    return s if len(s) <= n else s[:n] + "…"


def _short_url(u: str | None, n: int = 48) -> str:
    if not u:
        return ""
    return _trim(re.sub(r"^https?://(www\.)?", "", u), n)


def _frontmost_app() -> str | None:
    try:
        from AppKit import NSWorkspace
        a = NSWorkspace.sharedWorkspace().frontmostApplication()
        return str(a.localizedName()) if a else None
    except Exception:
        return None


def _ax_at(x: float, y: float) -> dict | None:
    """Accessibility element under a screen point: role + best-effort title."""
    if not _HAS_AX:
        return None
    try:
        sysw = _AXS.AXUIElementCreateSystemWide()
        err, el = _AXS.AXUIElementCopyElementAtPosition(sysw, float(x), float(y), None)
        if err != 0 or el is None:
            return None

        def attr(name):
            try:
                e, v = _AXS.AXUIElementCopyAttributeValue(el, name, None)
                return v if e == 0 else None
            except Exception:
                return None

        role = attr("AXRole")
        title = attr("AXTitle") or attr("AXDescription") or attr("AXValue")
        roledesc = attr("AXRoleDescription")
        # The app that OWNS the clicked element (via its pid) — authoritative,
        # unlike NSWorkspace.frontmostApplication() which is stale during app
        # switches / Spotlight and mislabels the click's app.
        app = None
        try:
            _e, pid = _AXS.AXUIElementGetPid(el, None)
            if pid:
                from AppKit import NSRunningApplication
                ra = NSRunningApplication.runningApplicationWithProcessIdentifier_(pid)
                if ra and ra.localizedName():
                    app = str(ra.localizedName())
        except Exception:
            pass
        t = "" if title is None else str(title)[:80]
        return {"role": str(role) if role else "", "title": t,
                "roledesc": str(roledesc) if roledesc else "", "app": app}
    except Exception:
        return None


# ── In-page DOM recorder (installed once per page, drained each poll) ──────────
_REC_JS = (
    "(function(){var K=window.__kinesis||(window.__kinesis={buf:[],hooked:false});"
    "function esc(s){try{return (window.CSS&&CSS.escape)?CSS.escape(s):s;}catch(e){return s;}}"
    "function sel(el){if(!el||el.nodeType!==1)return null;"
    "if(el.id&&/^[A-Za-z][\\w-]*$/.test(el.id))return '#'+el.id;"
    "var A=['data-testid','data-test','data-qa','aria-label','name'];"
    "for(var i=0;i<A.length;i++){var v=el.getAttribute&&el.getAttribute(A[i]);"
    "if(v)return el.tagName.toLowerCase()+'['+A[i]+'=\"'+esc(v)+'\"]';}"
    "var parts=[],node=el,depth=0;"
    "while(node&&node.nodeType===1&&depth<5){var part=node.tagName.toLowerCase();"
    "if(node.id&&/^[A-Za-z][\\w-]*$/.test(node.id)){parts.unshift('#'+node.id);break;}"
    "var p=node.parentNode;if(p){var same=[].filter.call(p.children,function(c){return c.tagName===node.tagName;});"
    "if(same.length>1)part+=':nth-of-type('+(1+[].indexOf.call(same,node))+')';}"
    "parts.unshift(part);node=p;depth++;}return parts.join(' > ');}"
    "function lab(el){var t=(el.getAttribute&&(el.getAttribute('aria-label')||el.getAttribute('placeholder')||el.getAttribute('title')))||'';"
    "if(!t&&el.labels&&el.labels.length)t=el.labels[0].innerText;"
    "if(!t&&el.id){var l=document.querySelector('label[for=\"'+el.id+'\"]');if(l)t=l.innerText;}"
    "return (t||'').trim().slice(0,80);}"
    "function own(el){var n=el,dep=0;while(n&&n.nodeType===1&&dep<7){var tg=n.tagName.toLowerCase();"
    "var rl=(n.getAttribute&&n.getAttribute('role'))||'';var cl=((n.className&&n.className.toString)?n.className.toString():'')||'';"
    "if(['li','tr','article','section','fieldset'].indexOf(tg)>=0||rl==='row'||rl==='listitem'||/(card|row|item|entry|tile|list-)/i.test(cl))"
    "return ((n.innerText||'')+'').trim().replace(/\\s+/g,' ').slice(0,140);n=n.parentNode;dep++;}return '';}"
    "function xp(el){if(!el||el.nodeType!==1)return '';var p=[];while(el&&el.nodeType===1){"
    "var ix=1,s=el.previousElementSibling;while(s){if(s.tagName===el.tagName)ix++;s=s.previousElementSibling;}"
    "p.unshift(el.tagName.toLowerCase()+'['+ix+']');el=el.parentNode;}return '/'+p.join('/');}"
    "function d(el){return {selector:sel(el),text:((el.innerText||el.value||'')+'').trim().replace(/\\s+/g,' ').slice(0,80),"
    "role:(el.getAttribute&&el.getAttribute('role'))||'',tag:el.tagName?el.tagName.toLowerCase():'',"
    "name:(el.getAttribute&&el.getAttribute('name'))||'',label:lab(el),octx:own(el),xpath:xp(el)};}"
    "if(!K.hooked){K.hooked=true;"
    "document.addEventListener('click',function(e){try{var o=d(e.target);o.type_='click';o.t=Date.now();o.url=location.href;K.buf.push(o);}catch(x){}},true);"
    "document.addEventListener('change',function(e){try{var el=e.target;if(el&&('value' in el)){var o=d(el);o.type_='input';o.value=((el.value||'')+'').slice(0,200);o.t=Date.now();o.url=location.href;K.buf.push(o);}}catch(x){}},true);"
    "document.addEventListener('keydown',function(e){try{var k=e.key;if(['Enter','Tab','Escape','ArrowUp','ArrowDown','ArrowLeft','ArrowRight'].indexOf(k)>=0){K.buf.push({type_:'key',key:k,selector:sel(e.target),t:Date.now(),url:location.href});}}catch(x){}},true);"
    "var st;window.addEventListener('scroll',function(){clearTimeout(st);st=setTimeout(function(){K.buf.push({type_:'scroll',sx:window.scrollX,sy:window.scrollY,t:Date.now(),url:location.href});},180);},true);}"
    "var out=K.buf.splice(0,K.buf.length);return JSON.stringify({url:location.href,title:document.title,events:out});})()"
)


def _as_escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')


def _osa(src: str):
    """Run AppleScript IN-PROCESS via NSAppleScript — ~4x faster than spawning
    `osascript` (no process startup, which is the dominant cost on the hot
    recording/replay paths). Returns (ok, text). Falls back to a subprocess so
    nothing regresses if NSAppleScript is unavailable."""
    try:
        from Foundation import NSAppleScript
        scpt = NSAppleScript.alloc().initWithSource_(src)
        res, err = scpt.executeAndReturnError_(None)
        if err is not None:
            return False, ""
        return True, ((res.stringValue() or "") if res is not None else "")
    except Exception:
        try:
            out = subprocess.run(["osascript", "-e", src], capture_output=True,
                                 text=True, timeout=5)
            return out.returncode == 0, (out.stdout or "").strip()
        except Exception:
            return False, ""


async def _osa_async(src: str):
    return await asyncio.to_thread(_osa, src)


def _chrome_js_sync(asname: str, js: str, timeout: float = 2.0) -> str | None:
    ok, r = _osa(f'tell application "{asname}" to execute front window\'s '
                 f'active tab javascript "{_as_escape(js)}"')
    return (r.strip() or None) if ok else None


def _chrome_tabs_sync(asname: str, timeout: float = 1.5):
    """Front window's (tab_count, active_index, active_url) — how new-tab and
    tab-switch actions are detected (browser chrome, not page DOM). Values are
    joined with `linefeed` because Chrome's own `tab` keyword shadows the tab
    character, and URLs never contain newlines."""
    script = (f'tell application "{asname}"\n'
              f'  set w to front window\n'
              f'  return ((count of tabs of w) as text) & linefeed & '
              f'(active tab index of w as text) & linefeed & (URL of active tab of w)\n'
              f'end tell')
    ok, out = _osa(script)
    if not ok:
        return None
    parts = out.strip().split("\n")
    if len(parts) < 3:
        return None
    try:
        return int(parts[0].strip()), int(parts[1].strip()), parts[2].strip()
    except Exception:
        return None


class _Recorder:
    def __init__(self):
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._tap = None
        self.recording = False
        self._raw: list[dict] = []          # native events (tap + AX)
        self._web: list[dict] = []          # web events (DOM poller)
        self._last_web_url = ""
        self._start_ts: float | None = None
        self._start_wall: float | None = None
        self._stop_requested = False
        self.error: str | None = None
        # AX enrichment worker
        self._ax_q: "queue.Queue[tuple]" = queue.Queue()
        self._ax_thread: threading.Thread | None = None
        self._ax_stop = False
        # DOM poller + browser-tab tracking
        self._dom_thread: threading.Thread | None = None
        self._dom_stop = False
        self._last_tab = None          # (tab_count, active_index, active_url)
        self._pending_open = False     # a new tab opened, awaiting its real URL
        # CDP recorder (real-time, lossless, multi-tab) — preferred when Chrome's
        # debug port is up; otherwise the osascript poller is used.
        self._cdp_thread: threading.Thread | None = None
        self._cdp_stop = False
        self._cdp_active = False
        self._cdp_count = 0
        self._cdp_rec = None

    # ── tap callback ─────────────────────────────────────────────────────────
    def _cb(self, proxy, etype, event, refcon):
        try:
            if etype in (Quartz.kCGEventTapDisabledByTimeout,
                         Quartz.kCGEventTapDisabledByUserInput):
                if self._tap:
                    Quartz.CGEventTapEnable(self._tap, True)
                return event

            if etype in (Quartz.kCGEventLeftMouseDown,
                         Quartz.kCGEventRightMouseDown):
                app = _frontmost_app()
                loc = Quartz.CGEventGetLocation(event)
                btn = "right" if etype == Quartz.kCGEventRightMouseDown else "left"
                idx = -1
                with self._lock:
                    if len(self._raw) < _MAX_STEPS:
                        self._raw.append({"kind": "click", "x": int(loc.x),
                                          "y": int(loc.y), "button": btn,
                                          "app": app, "t_wall": time.time()})
                        idx = len(self._raw) - 1
                if idx >= 0 and _HAS_AX:
                    self._ax_q.put((idx, float(loc.x), float(loc.y)))

            elif etype == Quartz.kCGEventKeyDown:
                keycode = Quartz.CGEventGetIntegerValueField(
                    event, Quartz.kCGKeyboardEventKeycode)
                flags = Quartz.CGEventGetFlags(event)
                mods = []
                if flags & Quartz.kCGEventFlagMaskCommand:   mods.append("command")
                if flags & Quartz.kCGEventFlagMaskControl:   mods.append("control")
                if flags & Quartz.kCGEventFlagMaskAlternate: mods.append("option")
                if flags & Quartz.kCGEventFlagMaskShift:     mods.append("shift")
                # Stop gesture (⌥⎋ or ⌘⎋) — always honored, never recorded.
                if keycode == 53 and ("option" in mods or "command" in mods):
                    self._stop_requested = True
                    return event
                is_chord = ("command" in mods) or ("control" in mods)
                # Never replay a quit (⌘Q).
                if is_chord and keycode == 12:
                    return event
                app = _frontmost_app()
                # Capture EVERY keystroke — Spotlight, native apps, and browsers
                # alike. Web-page typing is ALSO captured semantically by the DOM
                # poller; _build_steps reconciles the two and drops the native
                # duplicate. So nothing depends on a flaky "is a browser
                # frontmost?" check at capture time (Spotlight, overlays and app
                # switches all report the wrong frontmost app).
                special = _SPECIAL_KEYCODES.get(keycode)
                char = None
                try:
                    cnt, ustr = Quartz.CGEventKeyboardGetUnicodeString(event, 8, None, None)
                    if cnt and ustr:
                        char = ustr
                except Exception:
                    pass
                with self._lock:
                    if len(self._raw) < _MAX_STEPS:
                        self._raw.append({"kind": "key", "mods": mods, "special": special,
                                          "char": char, "keycode": int(keycode),
                                          "app": app, "t_wall": time.time()})
        except Exception as e:
            self.error = f"capture error: {str(e)[:100]}"
        return event

    # ── AX enrichment worker ─────────────────────────────────────────────────
    def _ax_loop(self):
        while True:
            try:
                idx, x, y = self._ax_q.get(timeout=0.25)
            except queue.Empty:
                if self._ax_stop:
                    return
                continue
            tgt = _ax_at(x, y)
            if tgt:
                with self._lock:
                    if 0 <= idx < len(self._raw):
                        self._raw[idx]["target"] = tgt

    # ── DOM poller ───────────────────────────────────────────────────────────
    def _web_append(self, ev: dict):
        with self._lock:
            if len(self._web) < _MAX_STEPS:
                self._web.append(ev)

    def _dom_loop(self):
        while not self._dom_stop:
            info = _browser_of(_frontmost_app())
            if not info or info[1] != "chrome":
                time.sleep(0.3)
                continue
            asname = info[0]
            # Drain in-page events (clicks / inputs / scroll / keys). Navigation
            # is NOT snapshotted here — it's derived at build time from these
            # events' own URLs, so it's tied to REAL interaction. A browser that's
            # merely frontmost (e.g. a background Gmail tab) never produces a
            # bogus "go to …" step.
            raw = _chrome_js_sync(asname, _REC_JS)
            if raw:
                try:
                    payload = json.loads(raw)
                except Exception:
                    payload = None
                if payload and not _is_friday_url(payload.get("url", "")):
                    for ev in payload.get("events", []):
                        if _is_friday_url(ev.get("url", "")):
                            continue
                        ev["app"] = asname
                        ev["t_wall"] = (ev.get("t") or 0) / 1000.0 or time.time()
                        self._web_append(ev)
            time.sleep(0.3)

    # ── CDP recorder thread (real-time, lossless, multi-tab) ─────────────────
    def _cdp_loop(self):
        import asyncio as _a
        from services import kinesis_cdp as _cdp

        async def run():
            rec = await _cdp.open_recorder()
            if not rec:
                return None
            self._cdp_rec = rec
            while not self._cdp_stop:
                self._cdp_count = len(rec.events)
                await _a.sleep(0.15)
            evs = rec.stop()
            await rec.close()
            return evs

        try:
            evs = _a.run(run())
        except Exception as ex:
            self.error = f"cdp record error: {str(ex)[:100]}"
            evs = None
        if evs:
            with self._lock:
                for e in evs:
                    e.setdefault("app", "Google Chrome")
                    if len(self._web) < _MAX_STEPS:
                        self._web.append(e)

    # ── tap run loop ─────────────────────────────────────────────────────────
    def _run_loop(self):
        mask = (Quartz.CGEventMaskBit(Quartz.kCGEventLeftMouseDown)
                | Quartz.CGEventMaskBit(Quartz.kCGEventRightMouseDown)
                | Quartz.CGEventMaskBit(Quartz.kCGEventKeyDown))
        tap = Quartz.CGEventTapCreate(
            Quartz.kCGSessionEventTap, Quartz.kCGHeadInsertEventTap,
            Quartz.kCGEventTapOptionListenOnly, mask, self._cb, None)
        if not tap:
            self.error = ("Could not start the recorder — grant Input Monitoring "
                          "(and Accessibility) to the app running Cosmos in "
                          "System Settings → Privacy & Security, then retry.")
            self.recording = False
            return
        self._tap = tap
        src = Quartz.CFMachPortCreateRunLoopSource(None, tap, 0)
        Quartz.CFRunLoopAddSource(Quartz.CFRunLoopGetCurrent(), src,
                                  Quartz.kCFRunLoopCommonModes)
        Quartz.CGEventTapEnable(tap, True)
        while not self._stop_requested and self.recording:
            Quartz.CFRunLoopRunInMode(Quartz.kCFRunLoopDefaultMode, 0.2, False)
        try:
            Quartz.CGEventTapEnable(tap, False)
        except Exception:
            pass
        self._tap = None
        self.recording = False

    # ── control ─────────────────────────────────────────────────────────────
    def start(self) -> dict:
        if self.recording:
            return {"ok": False, "error": "Already recording."}
        if not _HAS_QUARTZ:
            return {"ok": False, "error": "Recorder unavailable (Quartz missing)."}
        with self._lock:
            self._raw = []
            self._web = []
        self._last_web_url = ""
        self._last_tab = None
        self._pending_open = False
        self.error = None
        self._stop_requested = False
        self._start_ts = time.monotonic()
        self._start_wall = time.time()
        # workers
        self._ax_stop = False
        self._ax_thread = threading.Thread(target=self._ax_loop, daemon=True)
        self._ax_thread.start()
        # Web capture: CDP (real-time, all tabs) when the debug port is up, else
        # the osascript poller. Checked synchronously — start() runs on the event
        # loop thread where asyncio.run() would deadlock.
        self._cdp_active = False
        try:
            from services import kinesis_cdp as _cdp
            self._cdp_active = _cdp.is_available_sync()
        except Exception:
            self._cdp_active = False
        self._dom_stop = False
        self._cdp_stop = False
        self._cdp_count = 0
        if self._cdp_active:
            self._cdp_thread = threading.Thread(target=self._cdp_loop, daemon=True)
            self._cdp_thread.start()
        else:
            self._dom_thread = threading.Thread(target=self._dom_loop, daemon=True)
            self._dom_thread.start()
        # tap
        self.recording = True
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()
        time.sleep(0.18)
        if self.error:
            self._ax_stop = True
            self._dom_stop = True
            self._cdp_stop = True
            return {"ok": False, "error": self.error}
        return {"ok": True}

    def stop(self, via_button: bool = False) -> dict:
        was = self.recording or bool(self._raw) or bool(self._web)
        self._stop_requested = True
        self.recording = False
        if self._thread:
            self._thread.join(timeout=1.5)
        self._dom_stop = True
        if self._dom_thread:
            self._dom_thread.join(timeout=1.5)
        # CDP recorder needs a beat to drain buffered events + close the socket.
        self._cdp_stop = True
        if self._cdp_thread:
            self._cdp_thread.join(timeout=5.0)
        self._ax_stop = True
        if self._ax_thread:
            self._ax_thread.join(timeout=2.5)
        if not was:
            return {"ok": False, "error": "Not recording."}
        steps = self._build_steps()
        # A stop gesture that slipped in (Escape — bare or chorded) is never part
        # of the chore; drop any trailing ones.
        while steps and steps[-1].get("type") == "key" and \
                (steps[-1].get("special") == "escape" or steps[-1].get("keycode") == 53):
            steps = steps[:-1]
        # Trailing HUD Stop click (native) is an artifact — drop it.
        if via_button and steps and steps[-1]["type"] == "click" \
                and steps[-1].get("kind") == "native":
            steps = steps[:-1]
        steps = _optimize_steps(steps)     # collapse typo corrections into net text
        dur = int((time.monotonic() - self._start_ts) * 1000) if self._start_ts else 0
        return {"ok": True, "steps": steps, "count": len(steps), "duration_ms": dur}

    def status(self) -> dict:
        with self._lock:
            n = len(self._raw) + len(self._web)
        if self._cdp_active:               # CDP events buffer live, drained at stop
            n += self._cdp_count
        return {
            "recording": self.recording,
            "events": n,
            "engine": "cdp" if self._cdp_active else "applescript",
            "error": self.error,
            "elapsed_ms": int((time.monotonic() - self._start_ts) * 1000)
                          if (self.recording and self._start_ts) else 0,
        }

    # ── merge the two streams (wall-clock) → semantic steps ──────────────────
    def _build_steps(self) -> list[dict]:
        with self._lock:
            native = sorted(self._raw, key=lambda e: e.get("t_wall", 0))
            web = list(self._web)

        def native_text(e):
            m = e.get("mods") or []
            c = e.get("char")
            return (e.get("kind") == "key" and e.get("special") is None and bool(c)
                    and ord(c[0]) >= 32 and "command" not in m and "control" not in m)

        # Pre-form native typed-text RUNS from the native stream so a web event
        # landing mid-run (same timestamp) can't split a word into fragments.
        native2, j, m = [], 0, len(native)
        while j < m:
            ev = native[j]
            if native_text(ev):
                buf, first, last = ev["char"], ev["t_wall"], ev["t_wall"]
                j += 1
                while j < m and native_text(native[j]):
                    buf += native[j]["char"]; last = native[j]["t_wall"]; j += 1
                native2.append({"kind": "textrun", "text": buf, "t_wall": first, "last": last})
            else:
                native2.append(ev); j += 1

        combined = [("n", e) for e in native2] + [("w", e) for e in web]
        if not combined:
            return []
        combined.sort(key=lambda it: it[1].get("t_wall", 0))
        base = self._start_wall or combined[0][1].get("t_wall", 0)

        # Reconciliation indices — a browser action is captured BOTH natively (tap)
        # and semantically (DOM). These let us drop the native duplicate and keep
        # the semantic web version, while preserving native events (Spotlight,
        # native apps) that have no web counterpart.
        web_click_ts = [e["t_wall"] for e in web if e.get("type_") == "click"]
        web_input_tv = [(e["t_wall"], e.get("value") or "") for e in web if e.get("type_") == "input"]
        web_any_ts = [e["t_wall"] for e in web if e.get("type_") in ("click", "input", "key")]

        def clamp(dt): return max(0, min(int(dt * 1000), 60000))
        def near(ts, t, w): return any(abs(x - t) <= w for x in ts)
        def _nt(s): return re.sub(r"\s+", " ", (s or "")).strip().lower()
        def toverlap(a, b):
            a, b = _nt(a), _nt(b)
            return len(a) >= 1 and len(b) >= 1 and (a in b or b in a)

        steps: list[dict] = []
        prev = base
        last_nav = ""
        i, n = 0, len(combined)

        def lazy_nav(url, tw, app):
            # Emit a navigate ONLY as an anchor before a real interaction, when
            # the page changed — never just because a browser was frontmost.
            nonlocal last_nav, prev
            if url and url.startswith("http") and _norm_url(url) != last_nav:
                steps.append({"type": "navigate", "url": url, "app": app,
                              "delay_ms": clamp(tw - prev)})
                last_nav = _norm_url(url)
                prev = tw

        while i < n:
            src, e = combined[i]
            tw = e.get("t_wall", prev)
            if src == "n" and e.get("kind") == "click":
                tgt = e.get("target") or {}
                app = tgt.get("app") or e.get("app")   # AX-owning app is authoritative
                # Drop the native (coordinate) click if the SAME click was also
                # captured semantically by the DOM.
                if _browser_of(app) and near(web_click_ts, tw, 1.5):
                    i += 1; continue
                steps.append({"type": "click", "kind": "native", "x": e["x"], "y": e["y"],
                              "button": e["button"], "app": app,
                              "target": tgt, "delay_ms": clamp(tw - prev)})
                prev = tw; i += 1
            elif src == "n" and e.get("kind") == "textrun":
                # Typed into a web field → DOM captured it with a selector; drop
                # the raw native duplicate. Spotlight / native typing has no web
                # input nearby, so it survives.
                last = e.get("last", tw)
                dup = any(tw - 4 <= t <= last + 4 and toverlap(e["text"], v)
                          for t, v in web_input_tv)
                if not dup:
                    steps.append({"type": "type", "kind": "native", "text": e["text"],
                                  "delay_ms": clamp(tw - prev)})
                    prev = last
                i += 1
            elif src == "n":
                mods = e.get("mods") or []
                is_chord = ("command" in mods) or ("control" in mods)
                # A web page's own special keys (Enter/Tab in a form) are captured
                # by the DOM — drop the native duplicate. Chords (⌘T, ⌘A…) are
                # browser/app commands with no DOM analogue: always keep.
                if not is_chord and _browser_of(e.get("app")) and near(web_any_ts, tw, 1.5):
                    i += 1; continue
                steps.append({"type": "key", "kind": "native",
                              "key": e.get("special") or (e.get("char") or ""),
                              "special": e.get("special"), "char": e.get("char"),
                              "keycode": e.get("keycode"), "mods": mods,
                              "app": e.get("app"), "delay_ms": clamp(tw - prev)})
                prev = tw; i += 1
            else:  # web
                ty = e.get("type_")
                if ty in ("navigate", "open_tab", "switch_tab"):   # legacy macros
                    steps.append({"type": ty, "url": e.get("url"),
                                  "app": e.get("app"), "delay_ms": clamp(tw - prev)})
                    last_nav = _norm_url(e.get("url"))
                    prev = tw; i += 1
                elif ty == "click":
                    lazy_nav(e.get("url"), tw, e.get("app"))
                    steps.append({"type": "click", "kind": "web", "selector": e.get("selector"),
                                  "text": e.get("text"), "role": e.get("role"),
                                  "label": e.get("label"), "tag": e.get("tag"),
                                  "name": e.get("name"), "context": e.get("octx"),
                                  "xpath": e.get("xpath"), "app": e.get("app"),
                                  "url": e.get("url"), "delay_ms": clamp(tw - prev)})
                    prev = tw; i += 1
                elif ty == "input":
                    lazy_nav(e.get("url"), tw, e.get("app"))
                    steps.append({"type": "type", "kind": "web", "selector": e.get("selector"),
                                  "text": e.get("value", ""), "label": e.get("label"),
                                  "app": e.get("app"), "url": e.get("url"),
                                  "delay_ms": clamp(tw - prev)})
                    prev = tw; i += 1
                elif ty == "scroll":
                    lazy_nav(e.get("url"), tw, e.get("app"))
                    sx, sy, last = e.get("sx", 0), e.get("sy", 0), tw
                    i += 1
                    while i < n and combined[i][0] == "w" and combined[i][1].get("type_") == "scroll":
                        sx = combined[i][1].get("sx", sx)
                        sy = combined[i][1].get("sy", sy)
                        last = combined[i][1].get("t_wall", last)
                        i += 1
                    steps.append({"type": "scroll", "kind": "web", "sx": sx, "sy": sy,
                                  "app": e.get("app"), "url": e.get("url"),
                                  "delay_ms": clamp(tw - prev)})
                    prev = last
                elif ty == "key":
                    lazy_nav(e.get("url"), tw, e.get("app"))
                    steps.append({"type": "key", "kind": "web", "key": e.get("key"),
                                  "selector": e.get("selector"), "app": e.get("app"),
                                  "url": e.get("url"), "delay_ms": clamp(tw - prev)})
                    prev = tw; i += 1
                else:
                    i += 1
        return steps


_recorder = _Recorder()

def start_recording() -> dict:                 return _recorder.start()
def stop_recording(via_button=False) -> dict:  return _recorder.stop(via_button)
def recording_status() -> dict:                return _recorder.status()


# ─── Agent chat trigger — Cosmos runs a macro by name from chat/voice ─────────

async def _run_macro_tool(args, ctx) -> str:
    name = ((args or {}).get("name") or "").strip().lower()
    names = [m["name"] for m in list_macros()]
    macro = get_macro(name) if name else None
    if not macro:
        if not names:
            return "No macros are recorded yet — record one in the Kinesis tab first."
        return (f"No macro named '{name}'. Available macros: {', '.join(names)}. "
                "Call run_macro again with an exact name.")
    res = await replay(macro, params=(args or {}).get("params"))
    return res.get("message", "Replayed the macro.")


def register_agent_tool() -> None:
    """Register the `run_macro` tool so the agent can replay a saved macro from
    chat/voice ('run the reset-password macro'). Best-effort — never fatal."""
    try:
        from services import agent
        schema = {
            "name": "run_macro",
            "description": (
                "Run a saved Kinesis macro — a demonstrate-once recording of clicks, "
                "typing and browser actions the user captured on this Mac — by its exact "
                "name. Drives the real mouse/keyboard, so it is confirm-gated. If you omit "
                "or mis-name it, the available macro names are returned; retry with one."),
            "input_schema": {
                "type": "object",
                "properties": {
                    "name": {"type": "string",
                        "description": "exact macro name (kebab-case, e.g. 'reset-user-password')"},
                    "params": {"type": "object", "additionalProperties": {"type": "string"},
                        "description": "optional variable overrides for a parameterized macro, "
                                       "e.g. {\"query\": \"inception\"}"},
                },
                "required": ["name"],
            },
        }
        agent.register_tool(schema, _run_macro_tool, gate="confirm",
                            label="run macro", source="kinesis")
        agent.invalidate_tool_cache()
    except Exception as e:
        print(f"[kinesis] agent tool registration failed (non-fatal): {e}")


# ─── Understanding pass — infer the macro's core intent + name it ─────────────

async def understand(steps: list[dict]) -> dict:
    """Read the compiled steps and infer WHAT the macro accomplishes — its core
    intent — then propose a name/title/description. This is what lets Kinesis
    grasp the thing being taught, not just record it."""
    steps = steps or []
    if not steps:
        return {"ok": False, "error": "No steps to interpret."}
    lines = "\n".join(f"{i + 1}. {describe_step(s)}" for i, s in enumerate(steps))
    prompt = (
        'You are naming a recorded desktop-automation macro (a "Kinesis" macro on '
        'macOS). From its steps, infer the CORE INTENT — the real task it '
        'accomplishes for the user — and name it.\n\n'
        f"STEPS:\n{lines}\n\n"
        'Return ONLY a JSON object, no prose, no code fence:\n'
        '{"name": "kebab-case-slug (2-5 words)", "title": "Short Title Case Name", '
        '"description": "one sentence: what this macro does and when to run it"}')
    try:
        from services import llm, agent
        resp = await llm.acreate(model=agent.FAST_MODEL, fallbacks=llm.FAST_FALLBACKS,
                                 max_tokens=220,
                                 messages=[{"role": "user", "content": prompt}])
        txt = llm.extract_text(resp).strip()
        txt = re.sub(r"^```[a-z]*\n?|\n?```$", "", txt).strip()
        data = json.loads(txt)
        name = re.sub(r"[^a-z0-9-]", "",
                      (data.get("name") or "").lower().replace(" ", "-")).strip("-")[:41]
        return {"ok": True, "name": name,
                "title": (data.get("title") or "").strip()[:80],
                "description": (data.get("description") or "").strip()[:300]}
    except Exception as e:
        return {"ok": False, "error": str(e)[:140]}


# ─── Human-readable step summary ──────────────────────────────────────────────

def _optimize_steps(steps: list[dict]) -> list[dict]:
    """Collapse in-place corrections into their net result — the user typing
    "myflic", backspacing, then typing "xer" only ever meant "myflixer". Merges a
    contiguous run of type + backspace (on the same field) into one type of the
    final text, and drops any step that ends up empty."""
    out: list[dict] = []
    i, n = 0, len(steps)
    while i < n:
        s = steps[i]
        if s.get("type") == "type":
            buf = s.get("text", "")
            selector, kind = s.get("selector"), s.get("kind")
            j, changed = i + 1, False
            while j < n:
                nx = steps[j]
                if nx.get("type") == "key" and (nx.get("special") == "delete"
                                                or nx.get("keycode") == 51):
                    buf = buf[:-1]; j += 1; changed = True
                elif (nx.get("type") == "type" and nx.get("kind") == kind
                      and nx.get("selector") == selector):
                    buf += nx.get("text", ""); j += 1; changed = True
                else:
                    break
            if changed:
                out.append({**s, "text": buf})
                i = j
                continue
        out.append(s)
        i += 1
    return [s for s in out if not (s.get("type") == "type" and s.get("text", "") == "")]


def describe_step(st: dict) -> str:
    t, kind = st.get("type"), st.get("kind")
    if t == "navigate":
        return f"Go to {_short_url(st.get('url'))}"
    if t == "open_tab":
        return f"Open new tab → {_short_url(st.get('url'))}"
    if t == "switch_tab":
        return f"Switch to tab {_short_url(st.get('url'))}"
    if t == "click":
        if kind == "web":
            tgt = st.get("label") or st.get("text") or st.get("role") or st.get("selector") or "element"
            base = f"Click “{_trim(tgt, 40)}”"
            ctx = st.get("context")
            if ctx and _trim(ctx, 28) not in base:
                base += f" — in “{_trim(ctx, 30)}”"
            return base
        tg = st.get("target") or {}
        title = tg.get("title") or tg.get("roledesc") or ""
        base = f"Click {_trim(title, 40)}" if title else f"Click ({st.get('x')},{st.get('y')})"
        return base + (f" · {st['app']}" if st.get("app") else "")
    if t == "type":
        txt = st.get("text", "")
        show = txt if len(txt) <= 40 else txt[:40] + "…"
        where = st.get("label") or st.get("selector")
        return f'Type "{show}"' + (f" into {_trim(where, 26)}" if where and kind == "web" else "")
    if t == "scroll":
        return f"Scroll to {int(st.get('sy', 0))}px"
    if t == "key":
        if kind == "web":
            return f"Press {st.get('key')}"
        mods = st.get("mods") or []
        friendly = _BROWSER_CHORD_NAMES.get((tuple(sorted(mods)), st.get("keycode")))
        if friendly:
            return friendly
        label = str(st.get("special") or st.get("char") or st.get("key") or "")
        return "Press " + "+".join([*mods, label])
    return t or "step"


# ─── Persistence (~/.friday/macros/<name>.json) ───────────────────────────────

def _meta(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    steps = data.get("steps") or []
    return {"name": data.get("name", path.stem),
            "title": data.get("title") or path.stem.replace("-", " ").title(),
            "description": data.get("description", ""),
            "steps": len(steps), "duration_ms": data.get("duration_ms", 0),
            "created": data.get("created", "")}


def list_macros() -> list[dict]:
    out = []
    try:
        for f in sorted(MACROS_DIR.glob("*.json")):
            m = _meta(f)
            if m:
                out.append(m)
    except Exception as e:
        print(f"[kinesis] list failed: {e}")
    out.sort(key=lambda m: m.get("created", ""), reverse=True)
    return out


def get_macro(name: str) -> dict | None:
    name = (name or "").strip().lower()
    if not _NAME_RE.fullmatch(name):
        return None
    path = MACROS_DIR / f"{name}.json"
    try:
        if not path.exists() or path.parent != MACROS_DIR:
            return None
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def save_macro(name: str, title: str, description: str,
               steps: list[dict], duration_ms: int = 0) -> str:
    name = (name or "").strip().lower()
    if not _NAME_RE.fullmatch(name):
        return "Error: name must be kebab-case (letters/digits/dashes, 2-41 chars)."
    if not isinstance(steps, list) or not steps:
        return "Error: nothing to save — the recording had no steps."
    if len(steps) > _MAX_STEPS:
        return f"Error: too many steps ({len(steps)} > {_MAX_STEPS})."
    macro = {"name": name, "title": (title or name.replace("-", " ").title())[:80],
             "description": (description or "")[:400],
             "created": datetime.now().isoformat(timespec="seconds"),
             "duration_ms": int(duration_ms or 0), "steps": steps}
    path = MACROS_DIR / f"{name}.json"
    existed = path.exists()
    if not atomicio.write_json_atomic(path, macro, indent=1):
        return "Error: couldn't write the macro to disk."
    return f"{'Updated' if existed else 'Saved'} macro '{name}' ({len(steps)} steps)."


def update_macro(name: str, title=None, description=None, new_name=None,
                 params=None, steps=None) -> dict:
    """Edit a saved macro's title/description/variables/steps, and optionally
    rename it (which moves its file)."""
    name = (name or "").strip().lower()
    macro = get_macro(name)
    if not macro:
        return {"ok": False, "message": "No such macro."}
    if title is not None:
        macro["title"] = (title.strip()[:80] or macro.get("title") or name)
    if description is not None:
        macro["description"] = description.strip()[:400]
    if params is not None:
        clean = []
        for p in (params if isinstance(params, list) else [])[:20]:
            nm = re.sub(r"[^a-zA-Z0-9_-]", "", str(p.get("name") or "")).strip()[:40]
            if nm and p.get("value"):
                clean.append({"name": nm, "value": str(p.get("value"))[:200]})
        macro["params"] = clean
    if steps is not None and isinstance(steps, list) and steps:
        macro["steps"] = steps[:_MAX_STEPS]
    target = name
    if new_name:
        nn = new_name.strip().lower()
        if nn != name:
            if not _NAME_RE.fullmatch(nn):
                return {"ok": False, "message": "New name must be kebab-case (a-z, 0-9, dashes)."}
            if (MACROS_DIR / f"{nn}.json").exists():
                return {"ok": False, "message": f"A macro named '{nn}' already exists."}
            target = nn
            macro["name"] = nn
    try:
        dest = MACROS_DIR / f"{target}.json"
        # Write the new file FIRST; only unlink the old one once the new one is
        # safely on disk, or a failed rename would lose the macro entirely.
        if not atomicio.write_json_atomic(dest, macro, indent=1):
            return {"ok": False, "message": "Couldn't save — write to disk failed."}
        if target != name:
            (MACROS_DIR / f"{name}.json").unlink(missing_ok=True)   # remove the old file
    except Exception as e:
        return {"ok": False, "message": f"Couldn't save — {str(e)[:120]}"}
    return {"ok": True, "message": "Saved changes.", "name": target}


def delete_macro(name: str) -> str:
    name = (name or "").strip().lower()
    if not _NAME_RE.fullmatch(name):
        return "Error: bad macro name."
    path = MACROS_DIR / f"{name}.json"
    if not path.exists():
        return f"Error: no macro named '{name}'."
    try:
        path.unlink()
    except Exception as e:
        return f"Error: couldn't delete — {str(e)[:120]}"
    return f"Deleted macro '{name}'."


# ─── Replay ────────────────────────────────────────────────────────────────────

async def _chrome_js_async(sc, asname: str, js: str) -> str | None:
    # In-process AppleScript — the auto-wait retry can fire this ~20x for a slow
    # click, so avoiding a process spawn each time is a big replay speedup.
    ok, res = await _osa_async(
        f'tell application "{asname}" to execute front window\'s '
        f'active tab javascript "{_as_escape(js)}"')
    return res.strip() if ok else None


def _ok(res: str | None) -> bool:
    if not res:
        return False
    try:
        return bool(json.loads(res).get("ok"))
    except Exception:
        return False


async def _current_url(sc, asname: str, engine: str) -> str | None:
    if engine == "chrome":
        script = f'tell application "{asname}" to get URL of active tab of front window'
    else:
        script = f'tell application "{asname}" to get URL of front document'
    ok, res = await _osa_async(script)
    return res.strip() if ok and res.strip().startswith("http") else None


async def _wait_load(sc, asname: str, engine: str) -> None:
    for _ in range(14):
        await asyncio.sleep(0.25)
        if engine != "chrome":
            break
        ok, res = await _osa_async(
            f'tell application "{asname}" to get loading of active tab of front window')
        if ok and res.strip().lower() == "false":
            break
    await asyncio.sleep(0.4)


async def _navigate(sc, app: str, url: str) -> None:
    if not url:
        return
    info = _browser_of(app) or ("Google Chrome", "chrome")
    asname, engine = info
    await _osa_async(f'tell application "{asname}" to activate')
    await asyncio.sleep(0.15)
    cur = await _current_url(sc, asname, engine)
    if cur and _norm_url(cur) == _norm_url(url):
        return
    esc = _as_escape(url)
    if engine == "chrome":
        script = (f'tell application "{asname}"\n'
                  f'  if (count of windows) = 0 then make new window\n'
                  f'  set URL of active tab of front window to "{esc}"\n'
                  f'end tell')
    else:
        script = f'tell application "{asname}" to set URL of front document to "{esc}"'
    await _osa_async(script)
    await _wait_load(sc, asname, engine)


async def _open_tab(sc, app: str, url: str) -> None:
    """Open a NEW tab pointed straight at the URL — the semantic 'open a new
    tab' action, not a replayed Cmd+T keystroke."""
    info = _browser_of(app) or ("Google Chrome", "chrome")
    asname, engine = info
    await _osa_async(f'tell application "{asname}" to activate')
    await asyncio.sleep(0.12)
    esc = _as_escape(url or "")
    if engine == "chrome":
        script = (f'tell application "{asname}"\n'
                  f'  if (count of windows) = 0 then make new window\n'
                  f'  make new tab at end of tabs of front window with properties {{URL:"{esc}"}}\n'
                  f'end tell')
    else:
        script = f'tell application "{asname}" to make new document with properties {{URL:"{esc}"}}'
    await _osa_async(script)
    await _wait_load(sc, asname, engine)


async def _switch_tab(sc, app: str, url: str) -> None:
    """Activate an already-open tab showing this URL; if none, open it."""
    info = _browser_of(app) or ("Google Chrome", "chrome")
    asname, engine = info
    if engine != "chrome":
        return await _navigate(sc, app, url)
    esc = _as_escape(url or "")
    script = (f'tell application "{asname}" to tell front window\n'
              f'  set found to 0\n'
              f'  repeat with i from 1 to (count of tabs)\n'
              f'    if (URL of tab i) is "{esc}" then set found to i\n'
              f'  end repeat\n'
              f'  if found > 0 then set active tab index to found\n'
              f'  return found\n'
              f'end tell')
    ok, res = await _osa_async(script)
    if not (ok and res.strip().isdigit() and int(res.strip()) > 0):
        await _open_tab(sc, app, url)
    else:
        await asyncio.sleep(0.3)


def _click_js(selector, text, role, label, context="", xpath="") -> str:
    A = json.dumps({"s": selector or "", "t": text or "", "r": role or "",
                    "l": label or "", "c": context or "", "xp": xpath or ""})
    return ("(function(){var A=" + A + ";function norm(x){return ((x||'')+'').trim().replace(/\\s+/g,' ');}"
            "function own(el){var n=el,d=0;while(n&&n.nodeType===1&&d<8){var tg=n.tagName.toLowerCase();"
            "var rl=(n.getAttribute&&n.getAttribute('role'))||'';var cl=((n.className&&n.className.toString)?n.className.toString():'')||'';"
            "if(['li','tr','article','section','fieldset'].indexOf(tg)>=0||rl==='row'||rl==='listitem'||/(card|row|item|entry|tile|list-)/i.test(cl))return norm(n.innerText);n=n.parentNode;d++;}return '';}"
            "function ctxOk(el){if(!A.c)return true;var o=own(el);if(!o)return true;return o.indexOf(A.c.slice(0,40))>=0||A.c.indexOf(o.slice(0,40))>=0;}"
            "var el=A.s?document.querySelector(A.s):null;"
            # selector may now point at the wrong item (list re-rendered) → re-find by container context
            "if(el&&A.c&&!ctxOk(el))el=null;"
            "if(!el){var c=[].slice.call(document.querySelectorAll('a,button,[role=\"button\"],input,select,textarea,[onclick],li,td,span'));"
            "var bt=A.t?c.filter(function(n){var s=norm(n.innerText||n.value);return s===A.t||(A.t&&s.indexOf(A.t)>=0&&s.length<160);}):[];"
            "el=bt.filter(ctxOk)[0]||bt[0];"
            "if(!el&&A.l){var bl=c.filter(function(n){return n.getAttribute&&norm(n.getAttribute('aria-label')||n.getAttribute('placeholder'))===A.l;});el=bl.filter(ctxOk)[0]||bl[0];}}"
            "if(!el&&A.xp){try{var xr=document.evaluate(A.xp,document,null,9,null).singleNodeValue;if(xr)el=xr;}catch(e){}}"
            "if(!el)return JSON.stringify({ok:false});"
            "el.scrollIntoView({block:'center',inline:'center'});var r=el.getBoundingClientRect();var cx=r.left+r.width/2,cy=r.top+r.height/2;"
            "['pointerover','pointerdown','mousedown','pointerup','mouseup','click'].forEach(function(tp){el.dispatchEvent(new MouseEvent(tp,{bubbles:true,cancelable:true,view:window,clientX:cx,clientY:cy}));});"
            "return JSON.stringify({ok:true,text:norm(el.innerText||el.value).slice(0,60)});})()")


def _input_js(selector, value) -> str:
    A = json.dumps({"s": selector or "", "v": value or ""})
    return ("(function(){var A=" + A + ";var el=A.s?document.querySelector(A.s):null;if(!el)return JSON.stringify({ok:false});"
            "el.focus();try{var proto=el instanceof HTMLTextAreaElement?HTMLTextAreaElement.prototype:HTMLInputElement.prototype;"
            "var set=Object.getOwnPropertyDescriptor(proto,'value').set;set.call(el,A.v);}catch(e){el.value=A.v;}"
            "el.dispatchEvent(new Event('input',{bubbles:true}));el.dispatchEvent(new Event('change',{bubbles:true}));"
            "return JSON.stringify({ok:true});})()")


def _key_js(key, selector) -> str:
    A = json.dumps({"k": key or "Enter", "s": selector or ""})
    return ("(function(){var A=" + A + ";var el=A.s?document.querySelector(A.s):document.activeElement;if(!el)el=document.body;"
            "var o={bubbles:true,cancelable:true,key:A.k};['keydown','keypress','keyup'].forEach(function(tp){el.dispatchEvent(new KeyboardEvent(tp,o));});"
            "if(A.k==='Enter'&&el.form){try{el.form.requestSubmit?el.form.requestSubmit():el.form.submit();}catch(e){}}"
            "return JSON.stringify({ok:true});})()")


_CANDIDATES_JS = (
    "(function(){function norm(x){return ((x||'')+'').trim().replace(/\\s+/g,' ').slice(0,60);}"
    "function sel(el){if(el.id&&/^[A-Za-z][\\w-]*$/.test(el.id))return '#'+el.id;var p=[],n=el,d=0;"
    "while(n&&n.nodeType===1&&d<5){var pt=n.tagName.toLowerCase();var par=n.parentNode;"
    "if(par){var s=[].filter.call(par.children,function(x){return x.tagName===n.tagName;});"
    "if(s.length>1)pt+=':nth-of-type('+(1+[].indexOf.call(s,n))+')';}p.unshift(pt);n=par;d++;}return p.join(' > ');}"
    "function own(el){var n=el,d=0;while(n&&n.nodeType===1&&d<8){var tg=n.tagName.toLowerCase();"
    "var rl=(n.getAttribute&&n.getAttribute('role'))||'';var cl=((n.className&&n.className.toString)?n.className.toString():'')||'';"
    "if(['li','tr','article','section','fieldset'].indexOf(tg)>=0||rl==='row'||rl==='listitem'||/(card|row|item|entry|tile|list-)/i.test(cl))"
    "return ((n.innerText||'')+'').trim().replace(/\\s+/g,' ').slice(0,120);n=n.parentNode;d++;}return '';}"
    "var c=[].slice.call(document.querySelectorAll('a,button,[role=\"button\"],input,select,textarea,[onclick],li,[tabindex]'));"
    "var out=c.slice(0,80).map(function(el){return {sel:sel(el),text:norm(el.innerText||el.value),"
    "label:norm(el.getAttribute&&(el.getAttribute('aria-label')||el.getAttribute('placeholder'))),ctx:own(el),"
    "role:(el.getAttribute&&el.getAttribute('role'))||el.tagName.toLowerCase()};}).filter(function(x){return x.text||x.label;});"
    "return JSON.stringify(out);})()")


async def _web_click(sc, st, timeout: float = 8.0) -> bool:
    """Auto-wait: keep trying to find + click the element until it appears (the
    page may load slower on replay than it did while recording), up to timeout."""
    asname = st.get("app") or "Google Chrome"
    js = _click_js(st.get("selector"), st.get("text"), st.get("role"),
                   st.get("label"), st.get("context"), st.get("xpath"))
    end = time.monotonic() + timeout
    while True:
        if _ok(await _chrome_js_async(sc, asname, js)):
            return True
        if time.monotonic() >= end:
            return False
        await asyncio.sleep(0.35)   # element not ready yet — page still loading/rendering


async def _web_click_fallback(sc, st) -> bool:
    """Agent fallback: let a fast model pick the best-matching element from the
    page's live interactive elements when the recorded selector/text is gone."""
    asname = st.get("app") or "Google Chrome"
    raw = await _chrome_js_async(sc, asname, _CANDIDATES_JS)
    try:
        cands = json.loads(raw) if raw else []
    except Exception:
        cands = []
    if not cands:
        return False
    try:
        from services import llm, agent
        target = {"text": st.get("text"), "label": st.get("label"),
                  "role": st.get("role"), "selector": st.get("selector"),
                  "container_context": st.get("context")}
        prompt = ("A recorded macro must click a web element that moved or changed. "
                  "Original target (note `container_context` — the row/card it lived "
                  "in, which disambiguates identical buttons): " + json.dumps(target) +
                  ". Interactive elements now on the page, each with its own `ctx` "
                  "container text (JSON array): " + json.dumps(cands[:80]) + ". Pick the "
                  "element whose intent AND container match. Reply with ONLY its `sel` "
                  "string, or NONE. No prose, no quotes.")
        resp = await llm.acreate(model=agent.FAST_MODEL, fallbacks=llm.FAST_FALLBACKS,
                                 max_tokens=120, messages=[{"role": "user", "content": prompt}])
        pick = llm.extract_text(resp).strip().strip('`"\' ')
    except Exception:
        return False
    if not pick or pick.upper() == "NONE":
        return False
    return _ok(await _chrome_js_async(sc, asname, _click_js(pick, "", "", "", st.get("context"))))


async def _web_input(sc, st, timeout: float = 8.0) -> None:
    """Auto-wait for the field to exist before typing (slow loads on replay)."""
    asname = st.get("app") or "Google Chrome"
    js = _input_js(st.get("selector"), st.get("text", ""))
    end = time.monotonic() + timeout
    while True:
        if _ok(await _chrome_js_async(sc, asname, js)):
            return
        if time.monotonic() >= end:
            return
        await asyncio.sleep(0.3)


async def _web_scroll(sc, st) -> None:
    asname = st.get("app") or "Google Chrome"
    js = (f"(function(){{window.scrollTo({int(st.get('sx',0))},{int(st.get('sy',0))});"
          f"return JSON.stringify({{ok:true}});}})()")
    await _chrome_js_async(sc, asname, js)


async def _web_key(sc, st) -> None:
    asname = st.get("app") or "Google Chrome"
    await _chrome_js_async(sc, asname, _key_js(st.get("key"), st.get("selector")))


def _ax_press_at(x: float, y: float, expected_title: str = "") -> bool:
    """Press the Accessibility element under a screen point via AXPress — clicks
    the real control (works for toolbar/nested items a coordinate can miss) and
    verifies the title so a moved window doesn't press the wrong thing."""
    if not _HAS_AX:
        return False
    try:
        sysw = _AXS.AXUIElementCreateSystemWide()
        err, el = _AXS.AXUIElementCopyElementAtPosition(sysw, float(x), float(y), None)
        if err != 0 or el is None:
            return False

        def attr(n):
            try:
                e, v = _AXS.AXUIElementCopyAttributeValue(el, n, None)
                return v if e == 0 else None
            except Exception:
                return None

        if expected_title:
            title = attr("AXTitle") or attr("AXDescription") or attr("AXValue") or ""
            if str(title).strip().lower() != expected_title.strip().lower():
                return False
        e2, acts = _AXS.AXUIElementCopyActionNames(el, None)
        if e2 != 0 or not acts or "AXPress" not in acts:
            return False
        return _AXS.AXUIElementPerformAction(el, "AXPress") == 0
    except Exception:
        return False


async def _native_click(sc, st) -> bool:
    tgt = st.get("target") or {}
    app = st.get("app") or tgt.get("app") or ""
    title, role = tgt.get("title") or "", tgt.get("role") or ""
    # Bring the OWNING app to the front first — the click was recorded there, so
    # both the AX-by-name lookup and any coordinate fallback only land correctly
    # once that app (e.g. Notes) is frontmost.
    if app:
        await _osa_async(f'tell application "{app}" to activate')
        await asyncio.sleep(0.18)
    if app and title:
        et = _AXROLE_TO_SE.get(role, "button")
        try:
            ok, _ = await sc.click_ui_element(app, title, et)
            if ok:
                return True
        except Exception:
            pass
    # AXPress the real element under the recorded point (title-verified) — catches
    # toolbar / nested controls that System Events' window-1 search misses.
    if title and st.get("button", "left") == "left" and \
            await asyncio.to_thread(_ax_press_at, int(st.get("x", 0)), int(st.get("y", 0)), title):
        return True
    # coordinate fallback — always attempts the physical click
    action = {"left": "click", "right": "right_click"}.get(st.get("button", "left"), "click")
    await sc.mouse_control(action, int(st.get("x", 0)), int(st.get("y", 0)))
    return True


async def _replay_key(sc, st) -> None:
    mods = st.get("mods") or []
    is_chord = ("command" in mods) or ("control" in mods)
    app = st.get("app")
    # A shortcut (⌘T, ⌘L…) must land in the app it was pressed in — activate it
    # first so the browser genuinely opens the new tab / focuses the address bar.
    if app and (is_chord or _browser_of(app)):
        await _osa_async(f'tell application "{app}" to activate')
        await asyncio.sleep(0.12)
    using = ", ".join(f"{m} down" for m in mods)
    using_clause = f" using {{{using}}}" if using else ""
    special = st.get("special")
    keycode = st.get("keycode")
    if special and special in _NAME_TO_KEYCODE:
        script = (f'tell application "System Events" to key code '
                  f'{_NAME_TO_KEYCODE[special]}{using_clause}')
    elif is_chord and isinstance(keycode, int):
        # ⌘/⌃ chords replay most reliably by key code (⌘T=17, ⌘L=37, ⌘W=13…).
        script = (f'tell application "System Events" to key code '
                  f'{keycode}{using_clause}')
    else:
        ch = st.get("char") or st.get("key") or ""
        if not ch:
            return
        script = (f'tell application "System Events" to keystroke '
                  f'"{_as_escape(ch)}"{using_clause}')
    await _osa_async(script)
    if is_chord:                         # give the browser a beat to react
        await asyncio.sleep(0.35)


async def replay(macro: dict, speed: float = 1.0, params: dict | None = None) -> dict:
    """Drive the real mouse/keyboard (and live browser) through a macro's steps.
    Uses CDP for web steps when Chrome exposes the debug port (trusted dispatch,
    precise load waits, no per-step process spawn); falls back to AppleScript
    otherwise. `params` maps variable names → new values, substituted into typed
    text and URLs. Caller must serialize this against agent runs."""
    from services import system_control as sc
    steps = (macro or {}).get("steps") or []
    speed = max(0.25, min(float(speed or 1.0), 4.0))

    # Parameterization: replace each variable's recorded value with the value
    # supplied at run time (in typed text and in URLs).
    provided = params or {}
    repl = {}
    for d in (macro or {}).get("params") or []:
        dv, nv = d.get("value", ""), provided.get(d.get("name", ""), None)
        if dv and nv is not None and nv != dv:
            repl[dv] = nv
    if repl:
        def _sub(s):
            for a, b in repl.items():
                s = (s or "").replace(a, b)
            return s
        steps = [{**st,
                  **({"text": _sub(st.get("text", ""))} if st.get("type") == "type" else {}),
                  **({"url": _sub(st.get("url", ""))}
                     if st.get("type") in ("navigate", "open_tab", "switch_tab") else {})}
                 for st in steps]

    # Replay engine choice. By DEFAULT we drive the user's OWN Chrome (the
    # AppleScript path targets the real, logged-in foreground browser). The CDP
    # turbo path is faster but — because Chrome 136+ forbids remote-debugging the
    # default profile — it can only attach to the dedicated ~/.friday/chrome-cdp
    # profile, which is a fresh, signed-out window. Replaying there is almost
    # never what you want, so CDP replay is strictly OPT-IN via KINESIS_CDP_REPLAY.
    has_web = any(s.get("kind") == "web"
                  or s.get("type") in ("navigate", "open_tab", "switch_tab")
                  for s in steps)
    use_cdp = os.environ.get("KINESIS_CDP_REPLAY", "").strip().lower() in ("1", "true", "yes", "on")
    session = None
    if has_web and use_cdp:
        try:
            from services import kinesis_cdp as _cdp
            session = await _cdp.open_replay_session()
        except Exception:
            session = None

    # QUIESCE the Vision watchers for the whole replay. Their headless
    # watch-profile Chrome hijacks AppleScript targeting — 'tell app "Google
    # Chrome"' answers for the INVISIBLE poll browser, so replay steps drive a
    # windowless page and time out one by one. Close any preview session, wait
    # out an in-flight capture, and hold the capture lock until we're done.
    _watch_guard = None
    try:
        from services import watchers as _watchers
        if _watchers.session_active():
            await _watchers.stop_session()
        _watch_guard = _watchers._HEADLESS_LOCK
        await asyncio.wait_for(_watch_guard.acquire(), timeout=30)
    except Exception:
        _watch_guard = None      # watchers unavailable/slow — replay anyway

    done, failed = 0, 0
    fail_desc: list[str] = []
    try:
        for idx, st in enumerate(steps):
            delay = min(int(st.get("delay_ms", 0)), _REPLAY_DELAY_CAP_MS) / 1000.0
            if delay > 0:
                await asyncio.sleep(delay / speed)
            try:
                t, kind = st.get("type"), st.get("kind")
                if t == "navigate":
                    if session:
                        await session.navigate(st.get("url", ""))
                    else:
                        await _navigate(sc, st.get("app") or "Google Chrome", st.get("url", ""))
                elif t == "open_tab":
                    await _open_tab(sc, st.get("app") or "Google Chrome", st.get("url", ""))
                elif t == "switch_tab":
                    await _switch_tab(sc, st.get("app") or "Google Chrome", st.get("url", ""))
                elif t == "click":
                    if kind == "web":
                        ok = await session.click(st) if session else await _web_click(sc, st)
                        if not ok:
                            ok = await _web_click_fallback(sc, st)
                        if not ok:
                            raise RuntimeError("web element not found")
                    else:
                        await _native_click(sc, st)
                elif t == "type":
                    if kind == "web":
                        if session:
                            await session.type_into(st)
                        else:
                            await _web_input(sc, st)
                    else:
                        await sc.type_text(st.get("text", ""))
                elif t == "scroll":
                    if kind == "web":
                        if session:
                            await session.scroll(st)
                        else:
                            await _web_scroll(sc, st)
                elif t == "key":
                    if kind == "web":
                        if session:
                            await session.key(st)
                        else:
                            await _web_key(sc, st)
                    else:
                        await _replay_key(sc, st)
                done += 1
            except Exception:
                failed += 1
                fail_desc.append(f"#{idx + 1} {describe_step(st)}")
            await asyncio.sleep(0.12 / speed)
    finally:
        if _watch_guard is not None and _watch_guard.locked():
            try:
                _watch_guard.release()
            except Exception:
                pass
        if session:
            try:
                await session.close()
            except Exception:
                pass

    engine = "CDP" if session else "AppleScript"
    msg = f"Replayed {done} step{'s' if done != 1 else ''}"
    if failed:
        msg += f" — couldn't complete: {', '.join(fail_desc[:4])}"
    return {"ok": failed == 0, "done": done, "failed": failed,
            "failed_steps": fail_desc[:8],
            "message": f"{msg} via {engine}.", "engine": engine.lower()}
