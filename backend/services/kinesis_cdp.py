"""Kinesis CDP — Chrome DevTools Protocol acceleration (optional, opt-in).

When Chrome is launched with --remote-debugging-port, Kinesis records and replays
web steps over a persistent CDP WebSocket instead of polling `osascript`:

  - RECORD: a script installed via Page.addScriptToEvaluateOnNewDocument survives
    navigation and streams click/input/scroll/key events through a Runtime binding
    in REAL TIME — no 300ms poll gap, and nothing is lost when a page navigates.
  - REPLAY: elements are located in-page and clicked with a TRUSTED
    Input.dispatchMouseEvent; loads are awaited precisely — and there's no
    per-step process spawn.

Everything here is optional: if the debug port isn't available, callers fall back
to the AppleScript path. Single active-page scope in v1 (a good fit for console /
form chores); multi-tab is a future addition.
"""

import asyncio
import json
import os
import subprocess
import time

try:
    import httpx
    import websockets
    _HAS = True
except Exception:  # pragma: no cover
    _HAS = False

from services.kinesis import (_input_js, _norm_url, _is_friday_url,
                              _REPLAY_DELAY_CAP_MS)

DEBUG_PORT = 9222


# ── The in-page recorder: streams each event through the __kinesis binding.
_CDP_REC_JS = (
    "(function(){if(window.__kinesisHooked)return;window.__kinesisHooked=true;"
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
    "if(!t&&el.labels&&el.labels.length)t=el.labels[0].innerText;return (t||'').trim().slice(0,80);}"
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
    "function send(o){try{o.t=Date.now();o.url=location.href;window.__kinesis(JSON.stringify(o));}catch(e){}}"
    "document.addEventListener('click',function(e){var o=d(e.target);o.type_='click';send(o);},true);"
    "document.addEventListener('change',function(e){var el=e.target;if(el&&('value' in el)){var o=d(el);o.type_='input';o.value=((el.value||'')+'').slice(0,200);send(o);}},true);"
    "document.addEventListener('keydown',function(e){if(['Enter','Tab','Escape','ArrowUp','ArrowDown','ArrowLeft','ArrowRight'].indexOf(e.key)>=0)send({type_:'key',key:e.key,selector:sel(e.target)});},true);"
    "var st;window.addEventListener('scroll',function(){clearTimeout(st);st=setTimeout(function(){send({type_:'scroll',sx:window.scrollX,sy:window.scrollY});},180);},true);})()"
)


def _find_box_js(selector, text, role, label, context, xpath="") -> str:
    """Locate the element (selector → text → role → xpath, scoped by container
    context), scroll it into view, and return its viewport-center coords for a
    trusted CDP mouse dispatch — instead of a JS-synthesised click."""
    A = json.dumps({"s": selector or "", "t": text or "", "r": role or "",
                    "l": label or "", "c": context or "", "xp": xpath or ""})
    return ("(function(){var A=" + A + ";function norm(x){return ((x||'')+'').trim().replace(/\\s+/g,' ');}"
            "function own(el){var n=el,d=0;while(n&&n.nodeType===1&&d<8){var tg=n.tagName.toLowerCase();"
            "var rl=(n.getAttribute&&n.getAttribute('role'))||'';var cl=((n.className&&n.className.toString)?n.className.toString():'')||'';"
            "if(['li','tr','article','section','fieldset'].indexOf(tg)>=0||rl==='row'||rl==='listitem'||/(card|row|item|entry|tile|list-)/i.test(cl))return norm(n.innerText);n=n.parentNode;d++;}return '';}"
            "function ctxOk(el){if(!A.c)return true;var o=own(el);if(!o)return true;return o.indexOf(A.c.slice(0,40))>=0||A.c.indexOf(o.slice(0,40))>=0;}"
            "var el=A.s?document.querySelector(A.s):null;if(el&&A.c&&!ctxOk(el))el=null;"
            "if(!el){var c=[].slice.call(document.querySelectorAll('a,button,[role=\"button\"],input,select,textarea,[onclick],li,td,span'));"
            "var bt=A.t?c.filter(function(n){var s=norm(n.innerText||n.value);return s===A.t||(A.t&&s.indexOf(A.t)>=0&&s.length<160);}):[];"
            "el=bt.filter(ctxOk)[0]||bt[0];"
            "if(!el&&A.l){var bl=c.filter(function(n){return n.getAttribute&&norm(n.getAttribute('aria-label')||n.getAttribute('placeholder'))===A.l;});el=bl.filter(ctxOk)[0]||bl[0];}}"
            "if(!el&&A.xp){try{var xr=document.evaluate(A.xp,document,null,9,null).singleNodeValue;if(xr)el=xr;}catch(e){}}"
            "if(!el)return JSON.stringify({ok:false});"
            "el.scrollIntoView({block:'center',inline:'center'});var r=el.getBoundingClientRect();"
            "if(r.width===0&&r.height===0)return JSON.stringify({ok:false});"
            "return JSON.stringify({ok:true,x:r.left+r.width/2,y:r.top+r.height/2,text:norm(el.innerText||el.value).slice(0,60)});})()")


class CDPError(Exception):
    pass


class CDPSession:
    """A single CDP connection to one page target, with the canonical
    single-reader dispatch loop (routes replies by id, events to handlers)."""

    def __init__(self, ws, target: dict):
        self.ws = ws
        self.target = target
        self._id = 0
        self._pending: dict = {}
        self._reader: asyncio.Task | None = None
        self.events: list[dict] = []       # captured web events (recording)
        self._recording = False

    def _start(self):
        self._reader = asyncio.create_task(self._read_loop())

    async def _read_loop(self):
        try:
            async for raw in self.ws:
                m = json.loads(raw)
                mid = m.get("id")
                if mid is not None:
                    fut = self._pending.pop(mid, None)
                    if fut and not fut.done():
                        fut.set_result(m)
                elif m.get("method") == "Runtime.bindingCalled" \
                        and m.get("params", {}).get("name") == "__kinesis":
                    try:
                        ev = json.loads(m["params"]["payload"])
                        ev["t_wall"] = time.time()
                        if self._recording and not _is_friday_url(ev.get("url", "")):
                            self.events.append(ev)
                    except Exception:
                        pass
        except Exception:
            pass

    async def cmd(self, method: str, params: dict | None = None, timeout: float = 10.0):
        self._id += 1
        mid = self._id
        fut = asyncio.get_running_loop().create_future()
        self._pending[mid] = fut
        await self.ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
        m = await asyncio.wait_for(fut, timeout)
        if m.get("error"):
            raise CDPError(str(m["error"])[:160])
        return m.get("result", {})

    async def _eval(self, expr: str):
        r = await self.cmd("Runtime.evaluate", {"expression": expr, "returnByValue": True})
        return r.get("result", {}).get("value")

    # ── recording ────────────────────────────────────────────────────────────
    async def start_recording(self):
        await self.cmd("Page.enable")
        await self.cmd("Runtime.enable")
        await self.cmd("Runtime.addBinding", {"name": "__kinesis"})
        await self.cmd("Page.addScriptToEvaluateOnNewDocument", {"source": _CDP_REC_JS})
        await self.cmd("Runtime.evaluate", {"expression": _CDP_REC_JS})   # current doc too
        self.events = []
        self._recording = True

    def stop_recording(self) -> list[dict]:
        self._recording = False
        return list(self.events)

    # ── replay ────────────────────────────────────────────────────────────────
    async def navigate(self, url: str):
        cur = await self._eval("location.href")
        if cur and _norm_url(cur) == _norm_url(url):
            return
        await self.cmd("Page.navigate", {"url": url})
        for _ in range(40):
            await asyncio.sleep(0.15)
            if await self._eval("document.readyState") == "complete":
                break

    async def click(self, step: dict, timeout: float = 8.0) -> bool:
        js = _find_box_js(step.get("selector"), step.get("text"), step.get("role"),
                          step.get("label"), step.get("context"), step.get("xpath"))
        end = time.monotonic() + timeout
        while True:
            try:
                res = json.loads(await self._eval(js) or "{}")
            except Exception:
                res = {}
            if res.get("ok"):
                x, y = res["x"], res["y"]
                for typ in ("mousePressed", "mouseReleased"):
                    await self.cmd("Input.dispatchMouseEvent", {
                        "type": typ, "x": x, "y": y, "button": "left", "clickCount": 1})
                return True
            if time.monotonic() >= end:
                return False
            await asyncio.sleep(0.3)

    async def type_into(self, step: dict, timeout: float = 8.0) -> bool:
        js = _input_js(step.get("selector"), step.get("text", ""))
        end = time.monotonic() + timeout
        while True:
            try:
                if json.loads(await self._eval(js) or "{}").get("ok"):
                    return True
            except Exception:
                pass
            if time.monotonic() >= end:
                return False
            await asyncio.sleep(0.3)

    async def key(self, step: dict):
        k = step.get("key") or "Enter"
        # Trusted key dispatch (submits forms, triggers handlers reliably).
        for typ in ("keyDown", "keyUp"):
            await self.cmd("Input.dispatchKeyEvent", {"type": typ, "key": k,
                            "code": k, "windowsVirtualKeyCode": 13 if k == "Enter" else 0})

    async def scroll(self, step: dict):
        await self._eval(f"window.scrollTo({int(step.get('sx',0))},{int(step.get('sy',0))})")

    async def close(self):
        try:
            if self._reader:
                self._reader.cancel()
            await self.ws.close()
        except Exception:
            pass


class CDPRecorder:
    """Browser-level CDP connection that AUTO-ATTACHES to every page target —
    current tabs and any opened mid-recording — injects the recorder into each,
    and streams their events in real time. This is what makes CDP recording
    lossless AND multi-tab. Commands/events are routed per-target via sessionId
    (flattened protocol) over one socket."""

    def __init__(self, ws):
        self.ws = ws
        self._id = 0
        self._pending: dict = {}
        self._reader: asyncio.Task | None = None
        self.events: list[dict] = []
        self._recording = False
        self._sessions: dict = {}     # sessionId -> {url}

    def _start(self):
        self._reader = asyncio.create_task(self._read_loop())

    async def _read_loop(self):
        try:
            async for raw in self.ws:
                m = json.loads(raw)
                mid = m.get("id")
                if mid is not None:
                    fut = self._pending.pop(mid, None)
                    if fut and not fut.done():
                        fut.set_result(m)
                else:
                    await self._on_event(m)
        except Exception:
            pass

    async def _on_event(self, m):
        method = m.get("method")
        p = m.get("params", {})
        if method == "Target.attachedToTarget":
            info = p.get("targetInfo", {})
            if info.get("type") == "page":
                # Hook in a SEPARATE task — _hook sends commands and awaits their
                # replies, and only the reader reads replies, so awaiting here
                # would deadlock the reader against its own pending command.
                asyncio.create_task(self._hook(p.get("sessionId"), info.get("url", "")))
        elif method == "Runtime.bindingCalled" and p.get("name") == "__kinesis":
            try:
                ev = json.loads(p["payload"])
                ev["t_wall"] = time.time()
                if self._recording and not _is_friday_url(ev.get("url", "")):
                    self.events.append(ev)
            except Exception:
                pass
        elif method == "Target.detachedFromTarget":
            self._sessions.pop(p.get("sessionId"), None)

    async def _hook(self, sid: str, url: str):
        try:
            await self.cmd("Page.enable", session_id=sid)
            await self.cmd("Runtime.enable", session_id=sid)
            await self.cmd("Runtime.addBinding", {"name": "__kinesis"}, session_id=sid)
            await self.cmd("Page.addScriptToEvaluateOnNewDocument",
                           {"source": _CDP_REC_JS}, session_id=sid)
            await self.cmd("Runtime.evaluate", {"expression": _CDP_REC_JS}, session_id=sid)
            self._sessions[sid] = {"url": url}
        except Exception:
            pass

    async def cmd(self, method: str, params: dict | None = None,
                  session_id: str | None = None, timeout: float = 10.0):
        self._id += 1
        mid = self._id
        msg = {"id": mid, "method": method, "params": params or {}}
        if session_id:
            msg["sessionId"] = session_id
        fut = asyncio.get_running_loop().create_future()
        self._pending[mid] = fut
        await self.ws.send(json.dumps(msg))
        m = await asyncio.wait_for(fut, timeout)
        if m.get("error"):
            raise CDPError(str(m["error"])[:160])
        return m.get("result", {})

    async def start(self):
        await self.cmd("Target.setDiscoverTargets", {"discover": True})
        await self.cmd("Target.setAutoAttach",
                       {"autoAttach": True, "waitForDebuggerOnStart": False, "flatten": True})
        self.events = []
        self._recording = True

    def stop(self) -> list[dict]:
        self._recording = False
        return list(self.events)

    async def close(self):
        try:
            if self._reader:
                self._reader.cancel()
            await self.ws.close()
        except Exception:
            pass


class CDPReplaySession:
    """Browser-level replay connection with auto-attach. Follows every tab and
    routes each web step to the RIGHT tab — by matching the step's recorded URL,
    falling back to the most-recently-focused tab (so a ⌘T new tab, replayed as a
    keystroke, is picked up automatically). This is what makes CDP replay work
    across tabs, not just a single page."""

    def __init__(self, ws):
        self.ws = ws
        self._id = 0
        self._pending: dict = {}
        self._reader: asyncio.Task | None = None
        self._sessions: dict = {}     # sessionId -> {"url": ...}
        self._active: str | None = None

    def _start(self):
        self._reader = asyncio.create_task(self._read_loop())

    async def _read_loop(self):
        try:
            async for raw in self.ws:
                m = json.loads(raw)
                mid = m.get("id")
                if mid is not None:
                    fut = self._pending.pop(mid, None)
                    if fut and not fut.done():
                        fut.set_result(m)
                else:
                    self._on_event(m)
        except Exception:
            pass

    def _on_event(self, m):
        method, p = m.get("method"), m.get("params", {})
        if method == "Target.attachedToTarget":
            info = p.get("targetInfo", {})
            if info.get("type") == "page":
                asyncio.create_task(self._setup(p.get("sessionId"), info.get("url", "")))
        elif method == "Page.frameNavigated":
            sid = m.get("sessionId")
            fr = p.get("frame", {})
            if sid and not fr.get("parentId"):
                self._sessions.setdefault(sid, {})["url"] = fr.get("url", "")
                self._active = sid
        elif method == "Target.detachedFromTarget":     # tab closed — drop stale session
            sid = p.get("sessionId")
            self._sessions.pop(sid, None)
            if self._active == sid:
                self._active = next(iter(self._sessions), None)

    async def _setup(self, sid: str, url: str):
        try:
            await self.cmd("Page.enable", sid)
            await self.cmd("Runtime.enable", sid)
            self._sessions[sid] = {"url": url}
            self._active = sid
        except Exception:
            pass

    async def cmd(self, method: str, sid: str | None = None,
                  params: dict | None = None, timeout: float = 10.0):
        self._id += 1
        mid = self._id
        msg = {"id": mid, "method": method, "params": params or {}}
        if sid:
            msg["sessionId"] = sid
        fut = asyncio.get_running_loop().create_future()
        self._pending[mid] = fut
        await self.ws.send(json.dumps(msg))
        m = await asyncio.wait_for(fut, timeout)
        if m.get("error"):
            raise CDPError(str(m["error"])[:160])
        return m.get("result", {})

    async def start(self):
        await self.cmd("Target.setDiscoverTargets", params={"discover": True})
        await self.cmd("Target.setAutoAttach",
                       params={"autoAttach": True, "waitForDebuggerOnStart": False,
                               "flatten": True})
        await asyncio.sleep(0.8)      # let existing tabs attach before the first step

    def _route(self, url: str | None) -> str | None:
        if url:
            for sid, info in self._sessions.items():
                if info.get("url") and _norm_url(info["url"]) == _norm_url(url):
                    return sid
        return self._active or next(iter(self._sessions), None)

    async def _eval(self, sid: str, expr: str):
        r = await self.cmd("Runtime.evaluate", sid, {"expression": expr, "returnByValue": True})
        return r.get("result", {}).get("value")

    async def navigate(self, url: str):
        sid = self._active or next(iter(self._sessions), None)
        if not sid or not url:
            return
        cur = await self._eval(sid, "location.href")
        if cur and _norm_url(cur) == _norm_url(url):
            return
        await self.cmd("Page.navigate", sid, {"url": url})
        for _ in range(40):
            await asyncio.sleep(0.15)
            if await self._eval(sid, "document.readyState") == "complete":
                break
        self._sessions.setdefault(sid, {})["url"] = url

    async def click(self, step: dict, timeout: float = 8.0) -> bool:
        sid = self._route(step.get("url"))
        if not sid:
            return False
        js = _find_box_js(step.get("selector"), step.get("text"), step.get("role"),
                          step.get("label"), step.get("context"), step.get("xpath"))
        end = time.monotonic() + timeout
        while True:
            try:
                res = json.loads(await self._eval(sid, js) or "{}")
            except Exception:
                res = {}
            if res.get("ok"):
                for typ in ("mousePressed", "mouseReleased"):
                    await self.cmd("Input.dispatchMouseEvent", sid,
                                   {"type": typ, "x": res["x"], "y": res["y"],
                                    "button": "left", "clickCount": 1})
                return True
            if time.monotonic() >= end:
                return False
            await asyncio.sleep(0.3)

    async def type_into(self, step: dict, timeout: float = 8.0) -> bool:
        sid = self._route(step.get("url"))
        if not sid:
            return False
        js = _input_js(step.get("selector"), step.get("text", ""))
        end = time.monotonic() + timeout
        while True:
            try:
                if json.loads(await self._eval(sid, js) or "{}").get("ok"):
                    return True
            except Exception:
                pass
            if time.monotonic() >= end:
                return False
            await asyncio.sleep(0.3)

    async def key(self, step: dict):
        sid = self._route(step.get("url"))
        if not sid:
            return
        k = step.get("key") or "Enter"
        for typ in ("keyDown", "keyUp"):
            await self.cmd("Input.dispatchKeyEvent", sid,
                           {"type": typ, "key": k, "code": k,
                            "windowsVirtualKeyCode": 13 if k == "Enter" else 0})

    async def scroll(self, step: dict):
        sid = self._route(step.get("url"))
        if sid:
            await self._eval(sid, f"window.scrollTo({int(step.get('sx',0))},{int(step.get('sy',0))})")

    async def close(self):
        try:
            if self._reader:
                self._reader.cancel()
            await self.ws.close()
        except Exception:
            pass


async def open_replay_session(port: int | None = None) -> "CDPReplaySession | None":
    if not _HAS:
        return None
    port = DEBUG_PORT if port is None else port
    try:
        async with httpx.AsyncClient(timeout=1.5) as c:
            ver = (await c.get(f"http://localhost:{port}/json/version")).json()
        ws_url = ver.get("webSocketDebuggerUrl")
        if not ws_url:
            return None
        ws = await websockets.connect(ws_url, max_size=None)
    except Exception:
        return None
    s = CDPReplaySession(ws)
    s._start()
    await s.start()
    return s


# ── connection / availability ─────────────────────────────────────────────────

async def _list_targets(port: int) -> list[dict]:
    async with httpx.AsyncClient(timeout=1.5) as c:
        r = await c.get(f"http://localhost:{port}/json")
        return r.json()


def is_available_sync(port: int | None = None) -> bool:
    """Synchronous availability probe — used from the recorder's (sync) start(),
    which runs on the event-loop thread where asyncio.run() would deadlock."""
    if not _HAS:
        return False
    port = DEBUG_PORT if port is None else port
    try:
        return bool(httpx.get(f"http://localhost:{port}/json", timeout=1.0).json())
    except Exception:
        return False


async def open_recorder(port: int | None = None) -> "CDPRecorder | None":
    """Browser-level connection with auto-attach (records all tabs)."""
    if not _HAS:
        return None
    port = DEBUG_PORT if port is None else port
    try:
        async with httpx.AsyncClient(timeout=1.5) as c:
            ver = (await c.get(f"http://localhost:{port}/json/version")).json()
        ws_url = ver.get("webSocketDebuggerUrl")
        if not ws_url:
            return None
        ws = await websockets.connect(ws_url, max_size=None)
    except Exception:
        return None
    rec = CDPRecorder(ws)
    rec._start()
    await rec.start()
    return rec


async def is_available(port: int | None = None) -> bool:
    if not _HAS:
        return False
    port = DEBUG_PORT if port is None else port
    try:
        return bool(await _list_targets(port))
    except Exception:
        return False


_CHROME_BIN = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
# Chrome 136+ disables remote-debugging on the DEFAULT profile (anti-malware), so
# turbo runs in a dedicated, persistent Kinesis profile launched ALONGSIDE the
# user's main Chrome — logins there stick across sessions.
_CDP_PROFILE = os.path.expanduser("~/.friday/chrome-cdp")


async def enable_debug_chrome() -> dict:
    """Open the dedicated turbo browser: a separate, persistent Chrome instance
    with the debug port. Does NOT touch the user's main Chrome."""
    if not _HAS:
        return {"ok": False, "message": "CDP libraries unavailable."}
    if await is_available():
        return {"ok": True, "message": "Turbo mode is already on."}
    try:
        os.makedirs(_CDP_PROFILE, exist_ok=True)
        subprocess.Popen(
            [_CHROME_BIN, f"--remote-debugging-port={DEBUG_PORT}",
             f"--user-data-dir={_CDP_PROFILE}", "--no-first-run",
             "--no-default-browser-check"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    except Exception as e:
        return {"ok": False, "message": f"Couldn't launch the turbo browser — {str(e)[:120]}"}
    for _ in range(24):
        await asyncio.sleep(0.5)
        if await is_available():
            return {"ok": True, "message":
                    "Turbo browser ready — a dedicated Kinesis Chrome window (Chrome blocks "
                    "debugging your main profile). Do your chores in THAT window; sign into "
                    "sites there once and it stays logged in."}
    return {"ok": False, "message": "Launched the turbo browser but the debug port didn't come up."}


async def open_session(port: int | None = None) -> CDPSession | None:
    """Attach to the most relevant page target (the user's active browsing tab —
    not devtools, extensions, or the Cosmos HUD)."""
    if not _HAS:
        return None
    port = DEBUG_PORT if port is None else port
    try:
        targets = await _list_targets(port)
    except Exception:
        return None
    pages = [t for t in targets if t.get("type") == "page"
             and t.get("webSocketDebuggerUrl")
             and not t.get("url", "").startswith(("devtools://", "chrome-extension://",
                                                  "chrome://"))
             and not _is_friday_url(t.get("url", ""))]
    if not pages:
        return None
    target = pages[0]
    try:
        ws = await websockets.connect(target["webSocketDebuggerUrl"], max_size=None)
    except Exception:
        return None
    s = CDPSession(ws, target)
    s._start()
    return s
