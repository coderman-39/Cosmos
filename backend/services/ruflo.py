"""Ruflo (claude-flow) adapter — the Panel's coordination + knowledge fabric.

Speaks MCP JSON-RPC over stdio to a `claude-flow mcp start` subprocess:
`swarm_init` / `agent_spawn` register the swarm in Ruflo's stores, and
`memory_store` persists shared knowledge into its semantic memory
(`~/.friday/ruflo/.swarm/memory.db`, embeddings + vector search).

Strictly best-effort: if the binary is missing or any call fails/times out,
the adapter flips to disabled and every call returns None — the Panel runs
standalone, identically. Nothing here may ever slow or break a panel run.
"""

import asyncio
import json
import os
import shutil
from pathlib import Path

# Where Ruflo keeps its state (it writes .claude-flow/ + .swarm/ under cwd).
STATE_DIR = Path.home() / ".friday" / "ruflo"

_CALL_TIMEOUT = 20.0      # per-RPC wall
_BOOT_TIMEOUT = 45.0      # first call includes server boot

_proc: asyncio.subprocess.Process | None = None
_lock: asyncio.Lock | None = None
_rpc_id = 0
_disabled = False         # sticky: one hard failure turns the adapter off
_initialized = False


def _resolve_bin() -> str:
    """Find a runnable claude-flow. Env override first, then PATH, then the
    known npx cache location. '' = unavailable."""
    env = os.getenv("RUFLO_BIN", "").strip()
    if env:
        return env if Path(env).exists() else ""
    on_path = shutil.which("claude-flow")
    if on_path:
        return on_path
    npx_root = Path.home() / ".npm" / "_npx"
    if npx_root.is_dir():
        for hit in sorted(npx_root.glob("*/node_modules/.bin/claude-flow")):
            return str(hit)
    return ""


def available() -> bool:
    return not _disabled and bool(_resolve_bin())


def _shutdown_sync() -> None:
    global _proc, _initialized
    if _proc and _proc.returncode is None:
        try:
            _proc.terminate()
        except ProcessLookupError:
            pass
    _proc, _initialized = None, False


async def _ensure_proc() -> bool:
    """Start + initialize the MCP server once. False = adapter unusable."""
    global _proc, _disabled, _initialized
    if _disabled:
        return False
    if _proc and _proc.returncode is None and _initialized:
        return True
    bin_ = _resolve_bin()
    if not bin_:
        _disabled = True
        print("[ruflo] no claude-flow binary — panel runs standalone")
        return False
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        _proc = await asyncio.create_subprocess_exec(
            bin_, "mcp", "start", cwd=str(STATE_DIR),
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL)
        _initialized = False
        resp = await _rpc("initialize", {
            "protocolVersion": "2024-11-05", "capabilities": {},
            "clientInfo": {"name": "cosmos-panel", "version": "1.0"},
        }, timeout=_BOOT_TIMEOUT, allow_uninitialized=True)
        _initialized = resp is not None
        if _initialized:
            info = (resp.get("result") or {}).get("serverInfo") or {}
            print(f"[ruflo] connected — {info.get('name')} {info.get('version')}")
        else:
            _disabled = True
            _shutdown_sync()
        return _initialized
    except Exception as e:
        print(f"[ruflo] start failed ({str(e)[:100]}) — panel runs standalone")
        _disabled = True
        _shutdown_sync()
        return False


async def _rpc(method: str, params: dict, timeout: float = _CALL_TIMEOUT,
               allow_uninitialized: bool = False) -> dict | None:
    """One JSON-RPC round-trip. None on any failure (and the adapter goes
    disabled on transport-level failures)."""
    global _rpc_id, _disabled
    if _proc is None or _proc.returncode is not None:
        return None
    if not _initialized and not allow_uninitialized:
        return None
    _rpc_id += 1
    rid = _rpc_id
    msg = json.dumps({"jsonrpc": "2.0", "id": rid, "method": method,
                      "params": params}) + "\n"
    try:
        _proc.stdin.write(msg.encode())
        await _proc.stdin.drain()
        while True:
            line = await asyncio.wait_for(_proc.stdout.readline(), timeout)
            if not line:
                raise RuntimeError("server exited")
            try:
                data = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue                      # server log noise on stdout
            if data.get("id") == rid:
                return data
    except asyncio.CancelledError:
        raise
    except Exception as e:
        print(f"[ruflo] rpc {method} failed ({str(e)[:100]}) — adapter off")
        _disabled = True
        _shutdown_sync()
        return None


def _tool_json(resp: dict | None) -> dict:
    """Unwrap a tools/call response's first text-content block as JSON."""
    try:
        text = resp["result"]["content"][0]["text"]
        return json.loads(text)
    except Exception:
        return {}


async def _call_tool(name: str, arguments: dict) -> dict:
    if _lock is None:
        globals()["_lock"] = asyncio.Lock()
    async with _lock:                          # serialize: one stdio pipe
        if not await _ensure_proc():
            return {}
        return _tool_json(await _rpc("tools/call",
                                     {"name": name, "arguments": arguments}))


# ─── Public API (all best-effort; {}/None on failure) ───────────────────────────

async def swarm_init(topology: str, max_agents: int, strategy: str) -> str:
    """Register the swarm in Ruflo. Returns swarmId or ''."""
    out = await _call_tool("swarm_init", {
        "topology": topology, "maxAgents": max_agents, "strategy": strategy})
    return str(out.get("swarmId") or "") if out.get("success") else ""


async def agent_spawn(agent_type: str, swarm_id: str = "") -> str:
    """Register one agent. Returns agentId or ''."""
    args: dict = {"agentType": agent_type}
    if swarm_id:
        args["swarmId"] = swarm_id
    out = await _call_tool("agent_spawn", args)
    return str(out.get("agentId") or "") if out.get("success") else ""


async def memory_store(key: str, value: str, namespace: str) -> bool:
    """Persist shared knowledge into Ruflo's semantic memory (embedded +
    vector-searchable across future swarms)."""
    out = await _call_tool("memory_store", {
        "key": key, "value": value[:8000], "namespace": namespace})
    return bool(out.get("success"))


async def swarm_shutdown(swarm_id: str = "") -> None:
    args = {"graceful": True, **({"swarmId": swarm_id} if swarm_id else {})}
    await _call_tool("swarm_shutdown", args)


def reset_for_tests() -> None:
    """Test hook: forget the sticky disabled state and any process."""
    global _disabled
    _shutdown_sync()
    _disabled = False
