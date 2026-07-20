"""Minimal MCP client — plugs external tool servers into the agent loop.

Servers are declared in ~/.friday/mcp.json (Claude-Desktop shape):

    {"mcpServers": {
        "gcal":    {"command": "npx", "args": ["-y", "some-calendar-mcp"]},
        "tracker": {"url": "https://tracker.example.com/mcp",
                    "headers": {"Authorization": "Bearer ${TRACKER_TOKEN}"}}
    }}

Optional per-server keys:
    "tools":   ["name", …]  — allowlist (default: everything the server offers)
    "timeout": 60           — per-call ceiling in seconds
    "enabled": false        — keep the entry but skip connecting

${VAR} in any string value is expanded from the environment, so secrets can
stay in .env instead of this file.

Each server's tools register into the agent loop as mcp__<server>__<tool>,
gated by MCP annotations: readOnlyHint → runs free, destructiveHint →
confirms in BOTH permission modes, anything else confirms in ask mode and is
always blocked in unattended (scheduled) runs.

Transports: stdio (local subprocess, newline-delimited JSON-RPC 2.0) and
streamable HTTP (remote; JSON or SSE responses). Connection failures are
per-server and non-fatal — Cosmos boots with whatever connected.
"""

import asyncio
import json
import os
import re
from collections import deque
from pathlib import Path

import httpx

from services import agent, llm

CONFIG_FILE = Path.home() / ".friday" / "mcp.json"
PROTOCOL_VERSION = "2025-06-18"

CONNECT_TIMEOUT_S = float(os.getenv("FRIDAY_MCP_CONNECT_TIMEOUT", "30"))
CALL_TIMEOUT_S    = float(os.getenv("FRIDAY_MCP_CALL_TIMEOUT", "60"))

# A server exposing hundreds of tools would bloat the (cached) prompt for every
# run — cap per server and say so in status; the "tools" allowlist picks winners.
MAX_TOOLS_PER_SERVER = int(os.getenv("FRIDAY_MCP_MAX_TOOLS", "60"))
_MAX_RESULT_CHARS = 20_000
# asyncio's default 64KB readline limit would kill big tool results mid-stream.
_STDIO_LIMIT = 10 * 1024 * 1024


# ─── Transports ────────────────────────────────────────────────────────────────

# Env vars a stdio server subprocess actually needs. Everything else —
# SLACK_USER_TOKEN, gateway keys, the whole .env — must NOT leak into every
# third-party npx process.
_ENV_PASSTHROUGH = ("PATH", "HOME", "USER", "LOGNAME", "SHELL", "TMPDIR",
                    "LANG", "LC_ALL", "TERM", "NODE_EXTRA_CA_CERTS")


class _StdioTransport:
    """Local subprocess speaking newline-delimited JSON-RPC on stdin/stdout."""

    def __init__(self, command: str, args: list[str], env: dict):
        self.command, self.args, self.env = command, args, env
        self.proc: asyncio.subprocess.Process | None = None
        self._pending: dict[int, asyncio.Future] = {}
        self._next_id = 0
        self._tasks: list[asyncio.Task] = []
        self._dead = False
        self.stderr_tail: deque[str] = deque(maxlen=5)

    async def start(self) -> None:
        env = {k: os.environ[k] for k in _ENV_PASSTHROUGH if k in os.environ}
        env.update({k: str(v) for k, v in self.env.items()})
        self.proc = await asyncio.create_subprocess_exec(
            self.command, *self.args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            limit=_STDIO_LIMIT)
        self._tasks = [asyncio.create_task(self._read_stdout()),
                       asyncio.create_task(self._drain_stderr())]

    @property
    def alive(self) -> bool:
        return (self.proc is not None and self.proc.returncode is None
                and not self._dead)

    def _write(self, msg: dict) -> None:
        self.proc.stdin.write((json.dumps(msg, ensure_ascii=False) + "\n").encode())

    async def _read_stdout(self) -> None:
        try:
            while True:
                line = await self.proc.stdout.readline()
                if not line:
                    break
                try:
                    msg = json.loads(line)
                except Exception:
                    continue                      # servers sometimes log to stdout
                if not isinstance(msg, dict):
                    continue
                if "method" in msg and "id" in msg:
                    # Server→client request (sampling, roots, …) — politely refuse
                    # so the server doesn't hang waiting on us.
                    try:
                        self._write({"jsonrpc": "2.0", "id": msg["id"],
                                     "error": {"code": -32601,
                                               "message": "not supported by cosmos"}})
                    except Exception:
                        pass
                    continue
                fut = self._pending.pop(msg.get("id"), None)
                if fut and not fut.done():
                    fut.set_result(msg)
        finally:
            # EOF / read error / server death: mark the transport dead (later
            # requests fail FAST instead of hanging to their full timeout) and
            # fail everything still waiting.
            self._dead = True
            err = RuntimeError(f"MCP server exited ({self.stderr_tail[-1][:120] if self.stderr_tail else 'no stderr'})")
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(err)
            self._pending.clear()

    async def _drain_stderr(self) -> None:
        try:
            while True:
                line = await self.proc.stderr.readline()
                if not line:
                    break
                text = line.decode(errors="replace").strip()
                if text:
                    self.stderr_tail.append(text)
        except Exception:
            pass

    async def request(self, method: str, params: dict, timeout: float) -> dict:
        if not self.alive:
            raise RuntimeError("MCP server process is not running")
        self._next_id += 1
        rid = self._next_id
        fut = asyncio.get_running_loop().create_future()
        self._pending[rid] = fut
        self._write({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        await self.proc.stdin.drain()
        try:
            return await asyncio.wait_for(fut, timeout=timeout)
        except (asyncio.TimeoutError, TimeoutError):
            raise TimeoutError(f"{method} timed out after {int(timeout)}s")
        finally:
            # Timeout AND cancellation (the agent's outer tool ceiling) must
            # both clean up — leaked entries pile up until server EOF.
            self._pending.pop(rid, None)

    async def notify(self, method: str, params: dict) -> None:
        if not self.alive:
            return
        self._write({"jsonrpc": "2.0", "method": method, "params": params})
        await self.proc.stdin.drain()

    async def close(self) -> None:
        for t in self._tasks:
            t.cancel()
        if self.proc is not None and self.proc.returncode is None:
            try:
                self.proc.terminate()
                await asyncio.wait_for(self.proc.wait(), timeout=3)
            except Exception:
                try:
                    self.proc.kill()
                except Exception:
                    pass


class _HttpTransport:
    """Streamable-HTTP transport: one POST per request; the response arrives as
    plain JSON or as an SSE stream carrying the matching JSON-RPC response."""

    def __init__(self, url: str, headers: dict | None):
        self.url = url
        self.extra_headers = headers or {}
        self.session_id: str | None = None
        self._next_id = 0
        # No redirects: custom credential headers (x-api-key etc.) would be
        # forwarded to whatever host the redirect names. MCP endpoints are
        # direct URLs; a redirecting one should fail loudly.
        self.client = httpx.AsyncClient(
            timeout=httpx.Timeout(30.0, read=150.0), follow_redirects=False,
            limits=httpx.Limits(max_keepalive_connections=10,
                                keepalive_expiry=120))

    async def start(self) -> None:
        pass

    @property
    def alive(self) -> bool:
        return True

    def _headers(self) -> dict:
        h = {"Accept": "application/json, text/event-stream",
             "Content-Type": "application/json",
             "MCP-Protocol-Version": PROTOCOL_VERSION,
             **self.extra_headers}
        if self.session_id:
            h["Mcp-Session-Id"] = self.session_id
        return h

    async def request(self, method: str, params: dict, timeout: float) -> dict:
        self._next_id += 1
        msg = {"jsonrpc": "2.0", "id": self._next_id, "method": method,
               "params": params}
        async with self.client.stream("POST", self.url, json=msg,
                                      headers=self._headers(),
                                      timeout=timeout) as resp:
            resp.raise_for_status()
            sid = resp.headers.get("mcp-session-id")
            if sid:
                self.session_id = sid
            if "text/event-stream" in resp.headers.get("content-type", ""):
                data_lines: list[str] = []
                async for line in resp.aiter_lines():
                    if line.startswith("data:"):
                        data_lines.append(line[5:].lstrip())
                    elif not line and data_lines:
                        try:
                            payload = json.loads("\n".join(data_lines))
                        except Exception:
                            payload = None
                        data_lines = []
                        if isinstance(payload, dict) and payload.get("id") == msg["id"]:
                            return payload
                raise RuntimeError("SSE stream ended without a response")
            body = await resp.aread()
            return json.loads(body)

    async def notify(self, method: str, params: dict) -> None:
        try:
            await self.client.post(
                self.url, json={"jsonrpc": "2.0", "method": method, "params": params},
                headers=self._headers(), timeout=10.0)
        except Exception:
            pass    # notifications are best-effort

    async def close(self) -> None:
        try:
            await self.client.aclose()
        except Exception:
            pass


# ─── Server ────────────────────────────────────────────────────────────────────

def _schema_wants_array(spec: dict) -> bool:
    """True when a property schema's declared type is (or is exclusively) array.
    anyOf/oneOf that also permits a scalar is NOT treated as array-only — the
    scalar is already valid there, so we must not rewrite it."""
    t = spec.get("type")
    if t == "array":
        return True
    if isinstance(t, list) and "array" in t and "string" not in t \
            and "number" not in t and "integer" not in t and "boolean" not in t:
        return True
    for key in ("anyOf", "oneOf"):
        branches = spec.get(key)
        if isinstance(branches, list) and branches and all(
                isinstance(b, dict) and b.get("type") == "array" for b in branches):
            return True
    return False


def _coerce_args(args: dict, input_schema: dict | None) -> dict:
    """Best-effort shape fix before tools/call: when the model supplies a scalar
    for a parameter the tool's inputSchema declares as an array, wrap it in a
    one-element list. Tracker-style servers (whose list_works `type` is
    array<enum:[issue,ticket]>) hard-reject a bare scalar with a 400
    'not of type array' — this closes that gap without relying on the model to
    get the wrapping right every time. Only widens top-level scalar→[scalar];
    never narrows or reshapes anything else, so it's a no-op for correct args."""
    if not isinstance(args, dict) or not isinstance(input_schema, dict):
        return args
    props = input_schema.get("properties")
    if not isinstance(props, dict):
        return args
    out = None
    for key, val in args.items():
        spec = props.get(key)
        if isinstance(spec, dict) and val is not None and not isinstance(val, list) \
                and _schema_wants_array(spec):
            if out is None:
                out = dict(args)
            out[key] = [val]
    return out if out is not None else args


def _is_blank(v) -> bool:
    """Empty/whitespace string, empty list/dict, or None — i.e. 'not set'."""
    if v is None:
        return True
    if isinstance(v, str):
        return not v.strip()
    if isinstance(v, (list, tuple, dict)):
        return len(v) == 0
    return False


def _strip_blanks(v):
    """Recursively remove blank members: blank strings/None drop out of dicts,
    blank elements drop out of lists. Non-blank scalars pass through."""
    if isinstance(v, dict):
        return {k: sv for k, sv in ((k, _strip_blanks(x)) for k, x in v.items())
                if not _is_blank(sv)}
    if isinstance(v, (list, tuple)):
        return [sv for sv in (_strip_blanks(x) for x in v) if not _is_blank(sv)]
    return v


def _prune_empty_args(args: dict, input_schema: dict | None) -> dict:
    """Drop blank optional arguments before tools/call. Models frequently pad a
    call with EVERY optional field set to an empty default — empty arrays, blank
    strings, and objects like {"after":"","before":""} or a cursor with an empty
    next_cursor. Empty arrays are harmless, but some servers validate the
    empty-but-present object/string fields and reject the whole call with an
    opaque 400 'bad_request'. This strips those blanks so only real filters
    reach the server.

    An object filter that loses a schema-`required` sub-field after stripping
    (e.g. a cursor whose only real content was a blank next_cursor) is dropped
    whole — a partial object would itself fail validation."""
    if not isinstance(args, dict):
        return args
    props = (input_schema or {}).get("properties") or {}
    out = {}
    for key, val in args.items():
        cleaned = _strip_blanks(val)
        if _is_blank(cleaned):
            continue
        spec = props.get(key)
        if isinstance(cleaned, dict) and isinstance(spec, dict):
            required = spec.get("required") or []
            if any(r not in cleaned for r in required):
                continue        # incomplete object → would 400 anyway
        out[key] = cleaned
    return out


# Some tracker-style servers' works.update / parts.update validate collection
# fields (owned_by) as a set/add/remove OBJECT, but their tool schema exposes
# owned_by as a bare array — a raw list 400s with:
#   {"type":"unexpected_json_type","actual":"array","expected":"object",
#    "field_name":"owned_by"}
# (verified live). This is field-level validation, so it's identical for issues
# and tickets. Wrap a plain list of user IDs as a full replacement
# {"set": [...]}; leave an already-object shape (model used set/add/remove) or
# any non-update tool untouched. UPDATE only — the create_* tools take a bare
# array and must not be reshaped.
_TRACKER_UPDATE_TOOLS = frozenset({"update_work", "update_part"})


def _tracker_update_fixup(is_tracker: bool, tool: str, args: dict) -> dict:
    if not is_tracker or tool not in _TRACKER_UPDATE_TOOLS or not isinstance(args, dict):
        return args
    ob = args.get("owned_by")
    if isinstance(ob, list):
        return {**args, "owned_by": {"set": ob}}
    return args


_EMAIL_RE = re.compile(r"[^\s@\"']+@[^\s@\"']+\.[^\s@\"']+")

# Appended to a tool's error ONLY when the arguments contained an email — the
# single most common cause of a detail-less provider 400 (many trackers reject
# an email in owned_by/created_by, which want a user ID). The raw provider error
# ("Bad Request") tells the model nothing; this nudges it to resolve first.
_ID_HINT = (" Hint: an argument looks like an email address, but this tool "
            "likely expects an internal ID there (owner/creator/assignee fields "
            "usually do). Resolve the person to an ID first — e.g. search the "
            "provider's users, or use a get_current_user-style tool for "
            "yourself — then retry with that ID.")


def _args_have_email(value) -> bool:
    """True if any string anywhere in the arguments looks like an email."""
    if isinstance(value, str):
        return bool(_EMAIL_RE.search(value))
    if isinstance(value, dict):
        return any(_args_have_email(v) for v in value.values())
    if isinstance(value, list):
        return any(_args_have_email(v) for v in value)
    return False


# A tool result signals failure either via our own "Error:" prefix (isError /
# JSON-RPC error) OR — for servers that return API failures as plain
# text without setting isError — via a recognizable 400 marker in the text.
_FAIL_MARKERS = ("bad request", "bad_request", "invalid_id",
                 "status 400", "validation error")


def _looks_like_failure(out: str) -> bool:
    if out.startswith("Error:"):
        return True
    low = out.lower()
    return any(m in low for m in _FAIL_MARKERS)


def _flatten_result(result: dict) -> str:
    """MCP tools/call result → the plain string the agent loop expects.
    Failures follow the house convention: 'Error:'-prefixed."""
    parts: list[str] = []
    for c in result.get("content") or []:
        t = c.get("type")
        if t == "text":
            parts.append(c.get("text") or "")
        elif t == "resource":
            r = c.get("resource") or {}
            parts.append(r.get("text") or f"[resource {r.get('uri', '')}]")
        elif t in ("image", "audio"):
            parts.append(f"[{t} content — not displayable inline]")
        else:
            try:
                parts.append(json.dumps(c, ensure_ascii=False, default=str)[:500])
            except Exception:
                pass
    out = "\n".join(p for p in parts if p).strip()
    if len(out) > _MAX_RESULT_CHARS:
        out = out[:_MAX_RESULT_CHARS] + f" …[truncated {len(out) - _MAX_RESULT_CHARS} chars]"
    if result.get("isError"):
        return "Error: " + (out or "MCP tool reported an error")
    return out or "(empty result)"


class MCPServer:
    def __init__(self, name: str, spec: dict):
        self.name = name
        self.spec = spec
        self.transport = None
        self.tools: list[dict] = []
        self.tool_schemas: dict[str, dict] = {}   # tool name → inputSchema
        self.dropped = 0            # tools hidden by MAX_TOOLS_PER_SERVER
        self.error = ""
        self.server_info: dict = {}

    @property
    def connected(self) -> bool:
        return self.transport is not None and not self.error

    async def connect(self) -> None:
        if self.spec.get("command"):
            self.transport = _StdioTransport(self.spec["command"],
                                             list(self.spec.get("args") or []),
                                             dict(self.spec.get("env") or {}))
        elif self.spec.get("url"):
            self.transport = _HttpTransport(self.spec["url"], self.spec.get("headers"))
        else:
            raise ValueError("server config needs 'command' (stdio) or 'url' (http)")
        await self.transport.start()
        init = await self.transport.request("initialize", {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "cosmos", "version": "3.0"},
        }, CONNECT_TIMEOUT_S)
        if "error" in init:
            raise RuntimeError(init["error"].get("message", "initialize failed"))
        self.server_info = (init.get("result") or {}).get("serverInfo") or {}
        await self.transport.notify("notifications/initialized", {})

        tools, cursor = [], None
        while True:
            resp = await self.transport.request(
                "tools/list", {"cursor": cursor} if cursor else {}, CONNECT_TIMEOUT_S)
            if "error" in resp:
                raise RuntimeError(resp["error"].get("message", "tools/list failed"))
            result = resp.get("result") or {}
            tools.extend(result.get("tools") or [])
            cursor = result.get("nextCursor")
            if not cursor:
                break
        allow = self.spec.get("tools")
        if allow:
            allowed = set(allow)
            tools = [t for t in tools if t.get("name") in allowed]
        self.dropped = max(0, len(tools) - MAX_TOOLS_PER_SERVER)
        self.tools = tools[:MAX_TOOLS_PER_SERVER]
        self.tool_schemas = {t.get("name"): (t.get("inputSchema") or {})
                             for t in self.tools if t.get("name")}

    async def call_tool(self, tool: str, args: dict) -> str:
        timeout = float(self.spec.get("timeout") or CALL_TIMEOUT_S)
        schema = self.tool_schemas.get(tool)
        args = _coerce_args(args or {}, schema)
        args = _prune_empty_args(args, schema)
        is_tracker = ("tracker" in self.name.lower()
                      or "tracker" in (self.server_info.get("name") or "").lower())
        args = _tracker_update_fixup(is_tracker, tool, args)
        try:
            resp = await self.transport.request(
                "tools/call", {"name": tool, "arguments": args}, timeout)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            out = f"Error: MCP {self.name}/{tool} failed — {llm.sanitize_error(e, 200)}"
        else:
            if "error" in resp:
                out = f"Error: {str(resp['error'].get('message', 'MCP error'))[:300]}"
            else:
                out = _flatten_result(resp.get("result") or {})
        # A provider failure (our "Error:" prefix, or a plain-text 400 from a
        # server that doesn't set isError) plus an email in the args
        # almost always means an ID field got an email — tell the model how to
        # recover instead of letting it retry blind.
        if _args_have_email(args) and _looks_like_failure(out):
            out += _ID_HINT
        return out

    async def close(self) -> None:
        if self.transport is not None:
            await self.transport.close()


# ─── Registry integration ──────────────────────────────────────────────────────

_SERVERS: dict[str, MCPServer] = {}


def _expand(value):
    """Recursively expand ${VAR} in config strings from the environment."""
    if isinstance(value, str):
        return os.path.expandvars(value)
    if isinstance(value, dict):
        return {k: _expand(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand(v) for v in value]
    return value


_ENV_PATH = Path(__file__).resolve().parent.parent / ".env"


def _refresh_env() -> None:
    """Re-read backend/.env before expanding ${VAR}s. Without this, a secret
    added to .env AFTER boot (then picked up via 'mcp reload' rather than a full
    restart) never reaches os.environ — so ${TRACKER_PAT} stays literal and the
    server gets a bad key (401). load_dotenv only ran once at startup; refresh
    it here so a reload actually sees new tokens."""
    try:
        from dotenv import load_dotenv
        load_dotenv(_ENV_PATH, override=True)
    except Exception:
        pass


def _load_config() -> dict:
    _refresh_env()
    try:
        cfg = json.loads(CONFIG_FILE.read_text())
    except FileNotFoundError:
        return {}
    except Exception as e:
        print(f"[mcp] bad config {CONFIG_FILE}: {e}")
        return {}
    servers = cfg.get("mcpServers")
    return _expand(servers) if isinstance(servers, dict) else {}


def _agent_tool_name(server: str, tool: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_-]", "_", f"mcp__{server}__{tool}")[:64]


def _gate_for(tool: dict, trusted: bool, read_names: frozenset = frozenset()) -> str:
    """Gate from MCP annotations + config. destructiveHint is always honored (it
    only ADDS confirmation). A tool runs free ('open') only for a TRUSTED server
    (\"trust\": true) AND either its readOnlyHint annotation is set OR the user
    listed it in the server's \"read_tools\" allowlist — the escape hatch for
    servers that ship no annotations, so their get_/list_/search
    tools don't prompt on every call. Everything else confirms."""
    ann = tool.get("annotations") or {}
    if ann.get("destructiveHint") is True:
        return "destructive"
    if trusted and (ann.get("readOnlyHint") is True
                    or (tool.get("name") or "") in read_names):
        return "open"
    return "confirm"


def _register_server_tools(server: MCPServer) -> int:
    n = 0
    trusted = bool(server.spec.get("trust"))
    read_names = frozenset(server.spec.get("read_tools") or [])
    for tool in server.tools:
        name = _agent_tool_name(server.name, tool.get("name") or "")
        if name in agent._DYNAMIC_TOOLS:
            print(f"[mcp] name collision — skipping {name}")
            continue
        schema = {
            "name": name,
            "description": (f"[{server.name} · MCP] "
                            + (tool.get("description") or "").strip())[:800],
            "input_schema": tool.get("inputSchema")
                            or {"type": "object", "properties": {}},
        }

        async def handler(args, ctx, _srv=server, _tool=tool.get("name")):
            return await _srv.call_tool(_tool, args or {})

        try:
            agent.register_tool(
                schema, handler,
                gate=_gate_for(tool, trusted, read_names),
                # +15s headroom over the transport's own ceiling: the inner
                # timeout must fire first so its specific error (not a generic
                # agent-side cancellation) reaches the model.
                timeout=float(server.spec.get("timeout") or CALL_TIMEOUT_S) + 15.0,
                artifact=True,
                label=f"{server.name}: {tool.get('name')}",
                source=f"mcp:{server.name}")
            n += 1
        except ValueError as e:
            print(f"[mcp] skipping tool {name}: {e}")
    return n


# connect_all mutates module state and the agent registry — boot racing a
# user-triggered reload must never interleave (orphan subprocesses, tools
# registered for servers that were just torn down).
_connect_lock = asyncio.Lock()


async def connect_all() -> str:
    """(Re)connect every configured server and register its tools. Per-server
    failures are recorded and reported, never raised. Returns a summary line.
    Serialized; cancellation-safe (cache is re-invalidated no matter what)."""
    async with _connect_lock:
        try:
            return await _connect_all_locked()
        finally:
            # Runs on success AND on cancellation mid-way: whatever subset of
            # register/unregister happened, the next request rebuilds the
            # tool array instead of serving a stale memo.
            agent.invalidate_tool_cache()


async def _connect_all_locked() -> str:
    cfg = _load_config()
    for name, srv in list(_SERVERS.items()):
        agent.unregister_tools(f"mcp:{name}")
        try:
            await srv.close()
        except Exception:
            pass
    _SERVERS.clear()
    if not cfg:
        return f"No MCP servers configured. Add them to {CONFIG_FILE}."
    lines = []
    for name, spec in cfg.items():
        if spec.get("enabled") is False:
            continue
        srv = MCPServer(name, spec)
        _SERVERS[name] = srv
        try:
            await asyncio.wait_for(srv.connect(), timeout=CONNECT_TIMEOUT_S * 2)
            n = _register_server_tools(srv)
            note = (f" ({srv.dropped} more hidden — use a \"tools\" allowlist)"
                    if srv.dropped else "")
            lines.append(f"{name}: {n} tools{note}")
        except asyncio.CancelledError:
            # Stopped mid-connect (shutdown, run cancelled): don't orphan the
            # half-started subprocess.
            try:
                await srv.close()
            except Exception:
                pass
            raise
        except Exception as e:
            srv.error = str(e)[:200] or type(e).__name__
            lines.append(f"{name}: FAILED — {srv.error}")
            try:
                await srv.close()
            except Exception:
                pass
    return "MCP servers — " + "; ".join(lines)


async def boot() -> None:
    """Startup: connect configured servers, then re-warm the prompt cache once
    (the tool-table change busted the provider cache). No config → silent no-op."""
    if not _load_config():
        return
    try:
        print(f"[mcp] {await connect_all()}")
    except Exception as e:
        print(f"[mcp] boot failed (non-fatal): {e}")
        return
    if any(s.connected for s in _SERVERS.values()) and \
       os.getenv("FRIDAY_PREWARM", "1").lower() not in ("0", "false", "no"):
        await agent.prewarm()


async def reload() -> str:
    summary = await connect_all()
    return summary + "\nNote: the LLM prompt cache re-warms on the next request."


def status() -> list[dict]:
    """Structured per-server status for the management UI. Merges the config
    (so disabled/never-connected servers still show) with live connections."""
    cfg = _load_config()
    names = list(dict.fromkeys(list(cfg.keys()) + list(_SERVERS.keys())))
    out = []
    for name in names:
        spec = cfg.get(name, {})
        srv = _SERVERS.get(name)
        trusted = bool(spec.get("trust"))
        reads = frozenset(spec.get("read_tools") or [])
        enabled = spec.get("enabled", True) is not False
        if srv is None:
            state = "disabled" if not enabled else "not connected"
            out.append({"name": name, "state": state, "enabled": enabled,
                        "trusted": trusted, "error": "", "info": "",
                        "transport": "http" if spec.get("url") else "stdio",
                        "tools": []})
            continue
        out.append({
            "name": name,
            "state": "error" if srv.error else "connected",
            "enabled": enabled, "trusted": trusted,
            "error": srv.error or "",
            "info": srv.server_info.get("name", ""),
            "transport": "http" if spec.get("url") else "stdio",
            "dropped": srv.dropped,
            "tools": [{"name": t.get("name", ""),
                       "description": (t.get("description") or "").strip()[:180],
                       "gate": _gate_for(t, trusted, reads)}
                      for t in srv.tools],
        })
    return out


def status_text() -> str:
    if not _SERVERS:
        return (f"No MCP servers connected. Configure them in {CONFIG_FILE} "
                "(shape: {\"mcpServers\": {\"name\": {\"command\": …} | "
                "{\"url\": …}}}), then run the mcp tool with action=reload.")
    lines = []
    for name, srv in _SERVERS.items():
        if srv.error:
            lines.append(f"[{name}] FAILED: {srv.error}")
            continue
        shown = ", ".join((t.get("name") or "?") for t in srv.tools[:20])
        more = f" (+{len(srv.tools) - 20} more)" if len(srv.tools) > 20 else ""
        hidden = f" — {srv.dropped} hidden by cap" if srv.dropped else ""
        info = srv.server_info.get("name") or "connected"
        lines.append(f"[{name}] {info} — {len(srv.tools)} tools: {shown}{more}{hidden}")
    return "\n".join(lines)
