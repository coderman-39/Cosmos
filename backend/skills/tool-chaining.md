# Tool chaining — never forget the connectors you already have

Many requests are really TWO+ capabilities glued together ("take a photo **and**
mail it to Alice", "check the error **and** message the on-call", "grab that page
**and** put it in a doc"). The failure mode to avoid: doing the first half well,
then **forgetting you have a native connector for the second half** and either
stopping, asking the user, or falling back to slow GUI clicking.

**Before acting on any multi-step task, map each step to a tool.** Say it to
yourself: "step 1 = `take_photo`, step 2 = email → I have the `google` (Gmail)
connector wired via the refresh token, use that." Only if no native tool fits do
you drop to Chrome automation, then vision.

## Capability map — reach for these FIRST (API/native, no UI clicking)
- **Email / Calendar / Docs / Sheets / Meet** → `google` (Gmail send/draft/search,
  calendar create, docs, sheets). Wired via GOOGLE_REFRESH_TOKEN — it's always there.
- **Slack** (message a person/channel, read, react, status, DnD) → `slack`;
  send an image/video to a DM → `slack_photo`.
- **GitHub** (repos, PRs, issues) → `github`.
  **Anything on a connected MCP server** (infra, SQL, tickets, etc.) → `mcp`.
- **Capture**: photo → `take_photo`, video → `record_video`, screen → `screenshot`.
- **Calendar/Reminders/Notes/Contacts/iMessage/Music** (local macOS) → the
  same-named tools. **Files** → `find_files`, `read_file`, `write_file`.
- **Memory**: `remember_fact` / `recall_history` — use them so you don't re-ask.

Prefer every tool above over GUI automation (`click_ui`, `keystroke`, `type_text`,
`mouse`, `see_screen`) — those are the LAST resort, only when no connector fits or
the connector errors.

## Chaining recipes
- **"take a photo and mail it to X"** → `take_photo` (get the path). Then:
  - Photo must be **attached** → Gmail's API can't attach. `open_url`
    mail.google.com compose, write the mail, `gmail_attach(path)`, send.
  - Just a text mail (no attachment needed) → `google` service=gmail
    action=send/draft — one call, no browser.
- **"take a photo and send it to X on Slack"** → `take_photo` →
  `slack_photo(recipient=X, image_path=<path>, caption=...)`. One hop, no UI.
- **"screenshot this and email/post it"** → `screenshot` → `gmail_attach` (email)
  or `slack_photo` (Slack).
- **"look at the error and tell the on-call"** → `see_screen`/`read_app` to read,
  then `slack` send (don't ask the user to relay it).
- **"summarise this page into a doc / sheet"** → `fetch_url`/`read_browser` →
  `google` docs.create / sheets.write.
- **"who is X and draft them a note"** → `dossier`/`contacts`/`slack` read →
  `google` gmail draft or `slack` send.

## Rules
- Inventory tools **before** the first action, not after you've half-done it the slow way.
- One native connector call beats ten GUI clicks — and never fakes the result.
- If a connector genuinely errors (API disabled, scope), THEN fall back
  (Chrome → vision) and say so; don't silently skip a working connector.
- Sends/writes still pause for confirmation as normal — chaining doesn't bypass the gate.
