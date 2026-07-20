"""Google Workspace client — Gmail, Calendar, Docs, Sheets, Meet.

Auth is a single OAuth2 refresh token (GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET
/ GOOGLE_REFRESH_TOKEN in .env — generate via the OAuth Playground with "use
your own credentials"). The refresh token is exchanged for a short-lived access
token, cached until ~1 min before expiry.

This is Cosmos's PRIMARY path for Google Workspace — faster and more reliable
than driving the web apps. When it can't be used (not configured, an API not
enabled in the Cloud project, a scope missing, or any failure) the caller falls
back to Chrome automation, then vision (see skills/google-workspace.md).

High-level helpers return (ok, human_text). Secrets are scrubbed from every
error string. Nothing raises out of the public functions.
"""

import asyncio
import base64
import os
import re
import time
from email.mime.text import MIMEText

import httpx

CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID", "").strip()
CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET", "").strip()
REFRESH_TOKEN = os.getenv("GOOGLE_REFRESH_TOKEN", "").strip()
AVAILABLE = bool(CLIENT_ID and CLIENT_SECRET and REFRESH_TOKEN)

_TIMEOUT = float(os.getenv("GOOGLE_TIMEOUT", "25"))

_BASES = {
    "gmail":    "https://gmail.googleapis.com/gmail/v1",
    "calendar": "https://www.googleapis.com/calendar/v3",
    "docs":     "https://docs.googleapis.com/v1",
    "sheets":   "https://sheets.googleapis.com/v4",
    "drive":    "https://www.googleapis.com/drive/v3",
    "meet":     "https://meet.googleapis.com/v2",
}

_token = {"value": None, "exp": 0.0}
_secret_re = re.compile("|".join(re.escape(s) for s in (CLIENT_SECRET, REFRESH_TOKEN) if s)) \
    if (CLIENT_SECRET or REFRESH_TOKEN) else None


def reconfigure() -> None:
    """Re-read credentials from the environment (after the Connectors UI writes
    .env) so the change is live without a restart."""
    global CLIENT_ID, CLIENT_SECRET, REFRESH_TOKEN, AVAILABLE, _secret_re
    CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID", "").strip()
    CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET", "").strip()
    REFRESH_TOKEN = os.getenv("GOOGLE_REFRESH_TOKEN", "").strip()
    AVAILABLE = bool(CLIENT_ID and CLIENT_SECRET and REFRESH_TOKEN)
    _secret_re = (re.compile("|".join(re.escape(s) for s in (CLIENT_SECRET, REFRESH_TOKEN) if s))
                  if (CLIENT_SECRET or REFRESH_TOKEN) else None)
    _token.update({"value": None, "exp": 0.0})   # force a fresh token exchange


def _scrub(s: str, cap: int = 400) -> str:
    if _secret_re:
        s = _secret_re.sub("[redacted]", s)
    return s[:cap]


async def _access_token() -> str:
    now = time.time()
    if _token["value"] and _token["exp"] > now + 60:
        return _token["value"]
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        r = await client.post("https://oauth2.googleapis.com/token", data={
            "client_id": CLIENT_ID, "client_secret": CLIENT_SECRET,
            "refresh_token": REFRESH_TOKEN, "grant_type": "refresh_token"})
    if r.status_code != 200:
        raise RuntimeError(_scrub(f"token refresh HTTP {r.status_code}: {r.text}"))
    data = r.json()
    _token["value"] = data["access_token"]
    _token["exp"] = now + int(data.get("expires_in", 3600))
    return _token["value"]


def _hint(text: str) -> str:
    """Error suffix that steers the model down the fallback chain the user
    asked for: API → Chrome → vision."""
    return (text + "\n[FALLBACK] Google API path failed. Next, drive the web app "
            "in Chrome (open the Gmail/Calendar/Docs/Sheets URL, use browser_js / "
            "click_ui). If Chrome can't do it either, use vision (see_screen).")


async def _request(api: str, method: str, path: str,
                   params: dict | None = None, json_body: dict | None = None,
                   raw_result: bool = False):
    """One authenticated Google API call. Returns (ok, parsed|text)."""
    if not AVAILABLE:
        return False, _hint("Google isn't configured — set GOOGLE_CLIENT_ID / "
                            "GOOGLE_CLIENT_SECRET / GOOGLE_REFRESH_TOKEN in .env.")
    base = _BASES.get(api)
    if not base:
        return False, f"Unknown Google API '{api}'."
    try:
        token = await _access_token()
    except Exception as e:
        return False, _hint(f"Google auth failed: {_scrub(str(e))}")
    url = base + (path if path.startswith("/") else "/" + path)
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            r = await client.request(method.upper(), url,
                                     headers={"Authorization": f"Bearer {token}"},
                                     params=params, json=json_body)
    except Exception as e:
        return False, _hint(f"Google request failed: {_scrub(str(e))}")
    if r.status_code < 400:
        if raw_result:
            return True, (r.json() if r.content else {})
        return True, r
    # 403 "has not been used in project" = the API is disabled in the Cloud
    # console — a clear, actionable message plus the fallback steer.
    msg = _scrub(r.text, 300)
    if r.status_code == 403 and "has not been used" in r.text:
        msg = (f"the {api} API is not enabled in your Google Cloud project — "
               "enable it in the console, then retry. Original: " + msg)
    return False, _hint(f"Google {api} HTTP {r.status_code}: {msg}")


# ─── Gmail ─────────────────────────────────────────────────────────────────────

async def gmail_search(query: str, limit: int = 10) -> tuple[bool, str]:
    ok, r = await _request("gmail", "GET", "/users/me/messages",
                           params={"q": query or "", "maxResults": max(1, min(limit, 25))},
                           raw_result=True)
    if not ok:
        return False, r
    ids = [m["id"] for m in (r.get("messages") or [])]
    if not ids:
        return True, f"No Gmail messages match '{query}'."
    lines = []
    for mid in ids[:limit]:
        okm, meta = await _request(
            "gmail", "GET", f"/users/me/messages/{mid}",
            params={"format": "metadata",
                    "metadataHeaders": ["From", "Subject", "Date"]},
            raw_result=True)
        if not okm:
            continue
        h = {x["name"]: x["value"] for x in (meta.get("payload", {}).get("headers") or [])}
        snippet = (meta.get("snippet") or "").replace("\n", " ")[:120]
        lines.append(f"[{mid}] {h.get('Date','')[:16]} — {h.get('From','?')[:40]} — "
                     f"{h.get('Subject','(no subject)')[:60]} — {snippet}")
    return True, "\n".join(lines) or "No readable messages."


def _decode_part(part: dict) -> str:
    data = (part.get("body") or {}).get("data")
    if not data:
        return ""
    try:
        return base64.urlsafe_b64decode(data + "==").decode("utf-8", "replace")
    except Exception:
        return ""


def _extract_body(payload: dict) -> str:
    if not payload:
        return ""
    mt = payload.get("mimeType", "")
    if mt == "text/plain":
        return _decode_part(payload)
    if mt.startswith("multipart/"):
        for p in payload.get("parts") or []:
            if p.get("mimeType") == "text/plain":
                t = _decode_part(p)
                if t:
                    return t
        for p in payload.get("parts") or []:
            t = _extract_body(p)
            if t:
                return t
    if mt == "text/html":
        html = _decode_part(payload)
        return re.sub(r"<[^>]+>", " ", html)
    return ""


async def gmail_read(message_id: str) -> tuple[bool, str]:
    ok, r = await _request("gmail", "GET", f"/users/me/messages/{message_id}",
                           params={"format": "full"}, raw_result=True)
    if not ok:
        return False, r
    payload = r.get("payload", {})
    h = {x["name"]: x["value"] for x in (payload.get("headers") or [])}
    body = _extract_body(payload).strip()[:6000]
    return True, (f"From: {h.get('From','?')}\nTo: {h.get('To','?')}\n"
                  f"Date: {h.get('Date','?')}\nSubject: {h.get('Subject','(none)')}\n\n{body}")


def _parse_from(frm: str) -> tuple[str, str]:
    """'Alice Smith <alice@corp.com>' → ('Alice Smith', 'alice@corp.com')."""
    m = re.match(r'\s*"?([^"<]*)"?\s*<([^>]+)>', frm or "")
    if m:
        name = m.group(1).strip().strip('"')
        email = m.group(2).strip().lower()
        return (name or email.split("@")[0]), email
    f = (frm or "").strip()
    if "@" in f:
        return f.split("@")[0], f.lower()
    return f, ""


async def gmail_recent(days: int = 4, limit: int = 40,
                       with_body: bool = False) -> list[dict]:
    """Structured recent inbox messages for the dossier sweep. Returns
    [{id, date, from_name, from_email, subject, snippet, body}] — inbox only,
    excluding promotions/social noise. Bodies fetched only if with_body."""
    q = f"in:inbox newer_than:{max(1, days)}d -category:promotions -category:social"
    ok, r = await _request("gmail", "GET", "/users/me/messages",
                           params={"q": q, "maxResults": max(1, min(limit, 100))},
                           raw_result=True)
    if not ok:
        return []
    ids = [m["id"] for m in (r.get("messages") or [])][:limit]
    fmt = "full" if with_body else "metadata"
    base_params: dict = {"format": fmt}
    if not with_body:
        base_params["metadataHeaders"] = ["From", "Subject", "Date", "To"]
    sem = asyncio.Semaphore(6)   # parallel fetch — serial was ~0.3s/msg

    async def _one(mid: str) -> dict | None:
        async with sem:
            okm, meta = await _request("gmail", "GET", f"/users/me/messages/{mid}",
                                       params=base_params, raw_result=True)
        if not okm:
            return None
        payload = meta.get("payload", {})
        h = {x["name"]: x["value"] for x in (payload.get("headers") or [])}
        name, email = _parse_from(h.get("From", ""))
        snippet = (meta.get("snippet") or "").replace("\n", " ")[:220]
        body = _extract_body(payload).strip()[:2500] if with_body else ""
        return {"id": mid, "date": h.get("Date", "")[:31],
                "from_name": name, "from_email": email,
                "subject": h.get("Subject", "")[:160],
                "snippet": snippet, "body": body}

    results = await asyncio.gather(*[_one(mid) for mid in ids])
    return [x for x in results if x]


async def gmail_send(to: str, subject: str, body: str, cc: str = "",
                     draft: bool = False) -> tuple[bool, str]:
    msg = MIMEText(body or "", "plain", "utf-8")
    msg["To"] = to
    msg["Subject"] = subject or "(no subject)"
    if cc:
        msg["Cc"] = cc
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    if draft:
        ok, r = await _request("gmail", "POST", "/users/me/drafts",
                               json_body={"message": {"raw": raw}}, raw_result=True)
        return (True, f"Draft saved to {to}.") if ok else (False, r)
    ok, r = await _request("gmail", "POST", "/users/me/messages/send",
                           json_body={"raw": raw}, raw_result=True)
    if not ok:
        return False, r
    # Journal for promise-mining. Email can't be unsent via API → undoable=False.
    try:
        from services import outbox
        outbox.record("google", "gmail_send", target=to,
                      summary=f"{subject}: {body}"[:200], undoable=False)
    except Exception:
        pass
    return True, f"Email sent to {to}" + (f" (cc {cc})" if cc else "") + "."


# ─── Calendar / Meet ───────────────────────────────────────────────────────────

async def upcoming_events(max_results: int = 6, days: int = 1) -> list[dict]:
    """Structured upcoming events for the Nexus meetings lobe:
    [{summary, description, when}]. Empty list when calendar isn't wired."""
    import datetime
    now = datetime.datetime.now(datetime.timezone.utc)
    tmax = now + datetime.timedelta(days=max(1, days))
    ok, r = await _request("calendar", "GET", "/calendars/primary/events",
                           params={"timeMin": now.isoformat(), "timeMax": tmax.isoformat(),
                                   "singleEvents": "true", "orderBy": "startTime",
                                   "maxResults": max(1, min(max_results, 15))},
                           raw_result=True)
    if not ok:
        return []
    out = []
    for e in (r.get("items") or []):
        start = (e.get("start") or {}).get("dateTime") or (e.get("start") or {}).get("date", "")
        who = ", ".join((a.get("email", "") or "").split("@")[0]
                        for a in (e.get("attendees") or [])[:4] if not a.get("self"))
        when = start[11:16] if len(start) >= 16 else (start[:10] or "today")
        out.append({"summary": e.get("summary", "(untitled)"),
                    "description": (f"with {who}" if who else "on your calendar"),
                    "when": when})
    return out


async def calendar_list(days: int = 1, calendar_id: str = "primary") -> tuple[bool, str]:
    import datetime
    now = datetime.datetime.now(datetime.timezone.utc)
    tmax = now + datetime.timedelta(days=max(1, days))
    ok, r = await _request("calendar", "GET", f"/calendars/{calendar_id}/events",
                           params={"timeMin": now.isoformat(), "timeMax": tmax.isoformat(),
                                   "singleEvents": "true", "orderBy": "startTime",
                                   "maxResults": 25}, raw_result=True)
    if not ok:
        return False, r
    items = r.get("items") or []
    if not items:
        return True, f"No Google Calendar events in the next {days} day(s)."
    lines = []
    for e in items:
        start = (e.get("start") or {}).get("dateTime") or (e.get("start") or {}).get("date", "")
        who = ", ".join(a.get("email", "") for a in (e.get("attendees") or [])[:5])
        meet = e.get("hangoutLink", "")
        lines.append(f"[{e.get('id','')[:12]}] {start[:16]} — {e.get('summary','(untitled)')}"
                     + (f" — with {who}" if who else "") + (f" — {meet}" if meet else ""))
    return True, "\n".join(lines)


async def calendar_create(summary: str, start_iso: str, end_iso: str = "",
                          attendees: str = "", description: str = "",
                          with_meet: bool = False,
                          calendar_id: str = "primary") -> tuple[bool, str]:
    import datetime
    if not end_iso:
        try:
            s = datetime.datetime.fromisoformat(start_iso)
            end_iso = (s + datetime.timedelta(minutes=30)).isoformat()
        except Exception:
            return False, "Bad start time — use ISO 'YYYY-MM-DDTHH:MM:SS' (+offset)."
    event = {"summary": summary, "start": {"dateTime": start_iso},
             "end": {"dateTime": end_iso}}
    if description:
        event["description"] = description
    if attendees:
        event["attendees"] = [{"email": a.strip()} for a in attendees.split(",") if a.strip()]
    params = {"sendUpdates": "all"}
    if with_meet:
        event["conferenceData"] = {"createRequest": {
            "requestId": f"cosmos-{int(time.time())}",
            "conferenceSolutionKey": {"type": "hangoutsMeet"}}}
        params["conferenceDataVersion"] = 1
    ok, r = await _request("calendar", "POST", f"/calendars/{calendar_id}/events",
                           params=params, json_body=event, raw_result=True)
    if not ok:
        return False, r
    # Journal with the event id so undo_last can delete it.
    try:
        from services import outbox
        outbox.record("google", "calendar_create", target=summary,
                      summary=f"{summary} @ {start_iso}"[:200],
                      handle={"calendar_id": calendar_id, "event_id": r.get("id", "")},
                      undoable=bool(r.get("id")))
    except Exception:
        pass
    meet = f" Meet: {r.get('hangoutLink')}" if r.get("hangoutLink") else ""
    return True, f"Event '{summary}' created for {start_iso}.{meet} (id {r.get('id','')[:12]})"


async def calendar_delete(calendar_id: str, event_id: str) -> tuple[bool, str]:
    ok, r = await _request("calendar", "DELETE",
                           f"/calendars/{calendar_id}/events/{event_id}")
    return (True, "event deleted") if ok else (False, r)


async def meet_create() -> tuple[bool, str]:
    """Create a standalone Meet space (instant meeting link, no calendar event)."""
    ok, r = await _request("meet", "POST", "/spaces", json_body={}, raw_result=True)
    if not ok:
        return False, r
    return True, f"Meet ready: {r.get('meetingUri', r.get('name', '(no link)'))}"


# ─── Docs ──────────────────────────────────────────────────────────────────────

async def docs_create(title: str, text: str = "") -> tuple[bool, str]:
    ok, r = await _request("docs", "POST", "/documents",
                           json_body={"title": title or "Untitled"}, raw_result=True)
    if not ok:
        return False, r
    doc_id = r.get("documentId", "")
    if text:
        await _request("docs", "POST", f"/documents/{doc_id}:batchUpdate",
                       json_body={"requests": [{"insertText": {
                           "location": {"index": 1}, "text": text}}]}, raw_result=True)
    return True, (f"Doc '{title}' created: "
                  f"https://docs.google.com/document/d/{doc_id}/edit")


async def docs_read(document_id: str) -> tuple[bool, str]:
    ok, r = await _request("docs", "GET", f"/documents/{document_id}", raw_result=True)
    if not ok:
        return False, r
    out = []
    for el in (r.get("body", {}).get("content") or []):
        para = el.get("paragraph")
        if not para:
            continue
        for pe in para.get("elements") or []:
            out.append((pe.get("textRun") or {}).get("content", ""))
    return True, f"{r.get('title','(untitled)')}\n\n" + ("".join(out).strip()[:8000] or "(empty)")


async def docs_append(document_id: str, text: str) -> tuple[bool, str]:
    ok, r = await _request("docs", "GET", f"/documents/{document_id}", raw_result=True)
    if not ok:
        return False, r
    # end index = last content element's endIndex - 1 (before the final newline).
    content = r.get("body", {}).get("content") or []
    end = content[-1].get("endIndex", 2) - 1 if content else 1
    ok2, r2 = await _request("docs", "POST", f"/documents/{document_id}:batchUpdate",
                             json_body={"requests": [{"insertText": {
                                 "location": {"index": max(1, end)},
                                 "text": ("\n" + text)}}]}, raw_result=True)
    return (True, "Text appended to the doc.") if ok2 else (False, r2)


# ─── Sheets ────────────────────────────────────────────────────────────────────

async def sheets_read(spreadsheet_id: str, cell_range: str = "A1:Z50") -> tuple[bool, str]:
    ok, r = await _request("sheets", "GET",
                           f"/spreadsheets/{spreadsheet_id}/values/{cell_range}",
                           raw_result=True)
    if not ok:
        return False, r
    rows = r.get("values") or []
    if not rows:
        return True, f"{cell_range} is empty."
    return True, "\n".join(" | ".join(str(c) for c in row) for row in rows[:60])


async def sheets_write(spreadsheet_id: str, cell_range: str,
                       values: list) -> tuple[bool, str]:
    # values: a 2-D list of rows. Accept a flat list as a single row.
    if values and not isinstance(values[0], list):
        values = [values]
    ok, r = await _request("sheets", "PUT",
                           f"/spreadsheets/{spreadsheet_id}/values/{cell_range}",
                           params={"valueInputOption": "USER_ENTERED"},
                           json_body={"values": values}, raw_result=True)
    if not ok:
        return False, r
    return True, f"Wrote {r.get('updatedCells', '?')} cell(s) to {cell_range}."


async def sheets_create(title: str) -> tuple[bool, str]:
    ok, r = await _request("sheets", "POST", "/spreadsheets",
                           json_body={"properties": {"title": title or "Untitled"}},
                           raw_result=True)
    if not ok:
        return False, r
    sid = r.get("spreadsheetId", "")
    return True, (f"Sheet '{title}' created: "
                  f"https://docs.google.com/spreadsheets/d/{sid}/edit")


async def undo_action(entry: dict) -> tuple[bool, str]:
    """Inverse of a journaled Google action (for undo_last). Only calendar
    events are reversible; a sent email cannot be unsent via the API."""
    h = entry.get("handle") or {}
    if entry.get("action") == "calendar_create" and h.get("event_id"):
        return await calendar_delete(h.get("calendar_id", "primary"), h["event_id"])
    return False, f"no inverse for google action '{entry.get('action')}'"
