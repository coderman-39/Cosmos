"""Connector configuration — read integration status and set credentials from
the UI, persisting to backend/.env and hot-reconfiguring the live service.

NEVER returns a secret value — only whether each var is set. Writing goes to
backend/.env (upsert: update matching keys, append new, preserve everything
else), and the relevant service module re-reads env so the change is live
without a restart.
"""

import os
from pathlib import Path

from services import atomicio

ENV_PATH = Path(__file__).resolve().parent.parent / ".env"

# id → {label, blurb, note, fields:[{key,label,secret,optional}], via}
_INTEGRATIONS = [
    {"id": "google", "label": "Google Workspace",
     "blurb": "Gmail, Calendar, Docs, Sheets, Meet — one OAuth refresh token.",
     "note": "Generate via the OAuth Playground with your own credentials "
             "(scopes: calendar, documents, drive, meet, mail).",
     "fields": [
         {"key": "GOOGLE_CLIENT_ID", "label": "Client ID", "secret": False},
         {"key": "GOOGLE_CLIENT_SECRET", "label": "Client secret", "secret": True},
         {"key": "GOOGLE_REFRESH_TOKEN", "label": "Refresh token", "secret": True},
     ]},
    {"id": "slack", "label": "Slack",
     "blurb": "Read DMs, triage mentions, send messages, set status — as you.",
     "note": "A user token (xoxp-…) with the scopes Cosmos uses.",
     "fields": [
         {"key": "SLACK_USER_TOKEN", "label": "User token (xoxp-…)", "secret": True},
     ]},
]

# id → module attr with a reconfigure() that re-reads env (live, no restart).
_RECONFIGURE = {"google": "google", "slack": "slack"}


def _env_get(key: str) -> str:
    return (os.getenv(key) or "").strip()


def _configured(spec: dict) -> bool:
    required = [f["key"] for f in spec["fields"] if not f.get("optional")]
    return all(_env_get(k) for k in required)


def status() -> list[dict]:
    out = []
    for spec in _INTEGRATIONS:
        out.append({
            "id": spec["id"], "label": spec["label"], "blurb": spec["blurb"],
            "note": spec.get("note", ""), "via": spec.get("via", "native"),
            "configured": _configured(spec),
            "fields": [{"key": f["key"], "label": f["label"],
                        "secret": bool(f.get("secret")),
                        "optional": bool(f.get("optional")),
                        "set": bool(_env_get(f["key"]))}
                       for f in spec["fields"]],
        })
    return out


def _upsert_env(updates: dict) -> None:
    """Update/add KEY=value lines in backend/.env, preserving the rest. Also
    updates os.environ so status reflects the change immediately."""
    ENV_PATH.parent.mkdir(parents=True, exist_ok=True)
    try:
        lines = ENV_PATH.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        lines = []
    remaining = dict(updates)
    out = []
    for line in lines:
        m = line.split("=", 1)
        if len(m) == 2 and m[0].strip() in remaining:
            key = m[0].strip()
            out.append(f"{key}={remaining.pop(key)}")
        else:
            out.append(line)
    for key, val in remaining.items():
        out.append(f"{key}={val}")
    # Unique-temp atomic write (see services.atomicio): a fixed ".env.tmp" name
    # races any concurrent writer. Raise on failure — save() converts that into
    # an honest "Couldn't write .env", and os.environ must NOT advertise
    # credentials that never reached disk.
    if not atomicio.write_text_atomic(ENV_PATH, "\n".join(out) + "\n"):
        raise OSError(f"atomic write to {ENV_PATH} failed")
    for key, val in updates.items():
        os.environ[key] = val


def save(connector_id: str, values: dict) -> dict:
    """Persist non-empty values for a connector's fields, then hot-reconfigure
    the service. Returns {ok, message, live}."""
    spec = next((s for s in _INTEGRATIONS if s["id"] == connector_id), None)
    if spec is None:
        return {"ok": False, "message": f"Unknown connector '{connector_id}'."}
    allowed = {f["key"] for f in spec["fields"]}
    # Only write provided, non-empty values (blank = leave unchanged — so you
    # never have to re-type a secret to update a sibling field). Single-line.
    updates = {k: str(v).replace("\n", "").strip()
               for k, v in (values or {}).items()
               if k in allowed and str(v).strip()}
    if not updates:
        return {"ok": False, "message": "Nothing to save — fill at least one field."}
    try:
        _upsert_env(updates)
    except Exception as e:
        return {"ok": False, "message": f"Couldn't write .env — {str(e)[:120]}"}
    # Hot-reconfigure the live service so it works without a restart.
    live = False
    mod_name = _RECONFIGURE.get(connector_id)
    if mod_name:
        try:
            from importlib import import_module
            mod = import_module(f"services.{mod_name}")
            if hasattr(mod, "reconfigure"):
                mod.reconfigure()
                live = True
        except Exception:
            pass
    if spec.get("via") == "mcp":
        return {"ok": True, "live": False,
                "message": f"Saved. Reconnect the '{connector_id}' MCP server below to activate."}
    return {"ok": True, "live": live,
            "message": "Saved and active." if live
                       else "Saved to .env — restart the backend to activate."}
