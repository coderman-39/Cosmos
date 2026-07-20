# Google Workspace (Gmail · Calendar · Docs · Sheets · Meet)

**Fallback order — ALWAYS in this sequence:**
1. **`google` tool (API)** — the primary path. Fast, reliable, structured. Use it first for
   every Gmail / Calendar / Docs / Sheets / Meet task.
2. **Chrome** — only if the `google` tool errors (API disabled, missing scope, or any
   failure). Open the web app (`open_url` mail.google.com / calendar.google.com /
   docs.google.com / sheets.google.com), then drive it with `browser_js`, `click_web`,
   `click_ui`, `type_text`.
3. **Vision** — only if Chrome automation can't do it: `see_screen` to read the page, then
   `mouse`/`click_ui`/`keystroke` to act.
Never skip to Chrome/vision while the API is working — and never fake a result.

## `google` tool cheatsheet
- **Gmail**: `search(query)` (Gmail syntax: `from:alice is:unread newer_than:2d`) ·
  `read(id)` · `send(to,subject,body,cc)` · `draft(...)`. Sends confirm in ask mode.
- **Calendar**: `list(days)` · `create(summary,start,end,attendees,description,meet)`.
  start/end are full ISO-8601 datetime strings **including the local timezone offset** —
  resolve a phrase like "tomorrow 3pm" yourself into that ISO form. `meet:true`
  attaches a Google Meet link.
- **Docs**: `create(title,text)` → returns the doc URL · `read(id)` · `append(id,text)`.
- **Sheets**: `read(id,range)` (range like `Sheet1!A1:C10`) · `write(id,range,values)`
  (values = list of rows) · `create(title)`.
- **Meet**: `create()` → an instant standalone Meet link (no calendar event).

## Notes
- IDs: Gmail message ids, Doc/Sheet ids (from the URL), and calendar event ids all come
  from the matching `list`/`search`/`create` call — get them there, don't guess.
- A created calendar event is **undoable** (`undo_last` deletes it). A sent email is NOT —
  it's journaled for promise-tracking but can't be unsent.
- Two accounts: this uses whatever the GOOGLE_REFRESH_TOKEN belongs to. macOS Calendar.app
  (the `calendar` tool) is a separate, local view — prefer the `google` tool for the
  user's actual Google calendar.
- If the tool says an API "is not enabled in your Google Cloud project", tell the user to
  enable it in the console; meanwhile fall back to Chrome.
