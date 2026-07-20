# macOS Control

## Tool selection — cheapest path first

```
Open an app                    → open_app (never bash `open -a` via Terminal window)
Open file/folder in an app     → open_path (VS Code folders: open_path with app, NOT bash `code`)
Open a URL visibly             → open_url
Deep-link into an app          → bash `open "scheme://…"` (App Store, Spotify, Maps, Settings)
Run git/npm/brew silently      → bash
Anything else app/UI-level     → applescript (universal fallback)
Reading content:  read_browser (static pages) → browser_js (SPAs, structured extraction)
                  → read_app (native apps via Accessibility) → see_screen (last resort, vision)
```

SPAs (LinkedIn, Gmail, Slack web, YouTube) render via JS — URL fetch returns sparse HTML.
Use browser_js with CSS selectors, or see_screen if that fails.
Silent vs visible web: user said "open/show me/go to" → open_url. Otherwise answer silently
with web_search/fetch_url — never open Chrome just to answer a question.

## URL schemes

```
App Store search:      macappstores://search?q={query}
Spotify search:        spotify:search:{query}
Maps search:           maps://?q={query}
FaceTime:              facetime://{number_or_email}
Settings WiFi:         x-apple.systempreferences:com.apple.preference.network
Settings Bluetooth:    x-apple.systempreferences:com.apple.BluetoothSettings
Settings Display:      x-apple.systempreferences:com.apple.Displays-Settings.extension
Settings Sound:        x-apple.systempreferences:com.apple.preference.sound
```

## Take a photo ("take a photo", "selfie", "picture from the camera")

ALWAYS use the `take_photo` tool. It captures one fresh frame straight to a file and
returns the exact path. Do NOT use Photo Booth — it saves into a library bundle that
can't be reliably located, which leads to sending a STALE photo. Do NOT use `find` /
`mdfind` to hunt for the photo afterwards; `take_photo` already gives you the path.

- First-run setup: if `take_photo` returns `__NO_IMAGESNAP__`, run `brew install imagesnap`
  (confirmation is automatic), then retry. If it returns `__NO_CAMERA_PERM__`, tell the
  user to grant Camera permission to the terminal app once, then retry.

## Record a video ("record a video/clip", "take a video")

ALWAYS use the `record_video` tool (ffmpeg — auto-detects camera + mic, saves to a known
path). Do NOT shell out to raw ffmpeg or use Photo Booth/QuickTime UI. Default 5 seconds;
pass `duration` for longer. If it returns `__NO_FFMPEG__`, run `brew install ffmpeg`
(confirmation is automatic) then retry; `__NO_CAMERA_PERM__` → user grants Camera (and
Microphone) permission once. Send the resulting .mp4 to Slack with `slack_photo` (it
handles video files too).

## Send a photo/image to Slack

`slack_dm` sends TEXT only — it CANNOT attach a file. To send an image, use `slack_photo`
with the EXACT `image_path` (e.g. the path `take_photo` returned). Never send an old/other
image — always use the path from the capture you just made in this run.

Typical "take a photo and send it to me on Slack":
1. `take_photo`  → returns e.g. `~/Desktop/friday-photo-20260707_171200.jpg`
2. `slack_photo` with recipient="myself", image_path=<that exact path>

## Gmail send — ALWAYS the 2-step compose-URL pattern

1. open_url: `https://mail.google.com/mail/?view=cm&to=EMAIL&su=SUBJECT&body=BODY`
   (URL-encode subject and body)
2. Wait ~4 seconds for the page, then send with Cmd+Enter (Gmail's universal send shortcut):

```applescript
tell application "Google Chrome" to activate
delay 0.3
tell application "System Events"
    tell process "Google Chrome"
        set frontmost to true
        delay 0.2
        keystroke return using {command down}
    end tell
end tell
```

NEVER click the Send button by position or CSS selector. NEVER combine into one step.

### Attaching a file to a Gmail draft
Use the `gmail_attach` tool with the file path — ONE call. It finds the paperclip,
real-clicks it (a JS click is blocked by Chrome for file dialogs), and picks the
file in the Open dialog. A compose window must be open first (open_url the compose
URL). Repeat gmail_attach per file, then send with Cmd+Enter.
NEVER search the page with Cmd+F for the file, and never try to set the hidden
file <input> via JS — neither works.
For OTHER web uploads (Slack "+", forms): click the upload button, then `choose_file`.

## Clicking web pages — ticket boards, dashboards, sidebars

`click_web` clicks by exact visible text and now climbs to the nearest clickable
ancestor, so sidebar/nav items (e.g. a ticket board's "Sprints" tab) work even when the
label is a nested span. If a click reports NOTFOUND: the item may be off-screen
in a collapsed sidebar (open the sidebar first) or the label differs slightly —
`read_browser` to see the exact text, then click_web with it. Never fall back to
mouse coordinates for web pages.

## Slack DM — Cmd+K Quick Switcher (what slack_dm does)

Activate Slack → Cmd+K → type recipient → delay 0.6 → Return (select person) → delay 0.7 →
type the message EXACTLY as the user said it → Return to send. Sends to the person picked
in the switcher, not SlackBot. "myself"/"me" = the user's first name.

## Calendar events / reminders

Calendar AppleScript gives -600 if the app is closed — open via shell first, then wait:

```bash
open -a Calendar   # then sleep 2
```

```applescript
tell application "Calendar"
    set theDate to current date
    tell first calendar
        make new event at end with properties {summary:"TITLE", start date:theDate, end date:theDate + 1 * hours}
    end tell
    reload calendars
end tell
```

Events auto-sync to Google Calendar. Fallback: Reminders one-liner —
`tell application "Reminders" to make new reminder with properties {name:"TITLE"}`
Last resort: open `https://calendar.google.com/calendar/render?action=TEMPLATE&text=TITLE`.

## App not installed → Homebrew (prefer it over the App Store — scriptable, no sign-in)

Check first: `ls /Applications/AppName.app 2>/dev/null && echo EXISTS || echo MISSING`
Install (needs user confirmation): `brew install --cask <cask>`. Unknown cask: `brew search --cask <name>`.

| App | Cask | App | Cask |
|-----|------|-----|------|
| Sublime Text | sublime-text | Visual Studio Code | visual-studio-code |
| Google Chrome | google-chrome | Firefox | firefox |
| iTerm2 | iterm2 | Warp | warp |
| Slack | slack | Zoom | zoom |
| Docker Desktop | docker | Postman | postman |
| Figma | figma | Obsidian | obsidian |
| Notion | notion | Arc | arc |
| Brave Browser | brave-browser | Cursor | cursor |
| Rectangle | rectangle | 1Password | 1password |
| TablePlus | tableplus | Insomnia | insomnia |

## Useful CSS selectors (browser_js)

| Site | What | Selector |
|------|------|----------|
| LinkedIn | Job cards | `.job-card-container` |
| LinkedIn | Job title | `.job-card-list__title` |
| Gmail | Email rows | `tr.zA` |
| Gmail | Sender | `.yX.xY` |
| Slack web | Messages | `.c-message__body` |
| Any page | All text | `document.body.innerText` |

## Notes / native app tips

- Notes: `tell application "Notes" to make new note with properties {body:"…"}` — never Cmd+N + keystrokes.
- Electron apps (Slack, VS Code) often block read_app — fall back to see_screen.
- Failed with -1743 / "not allowed" → Accessibility permission missing; tell the user which
  System Settings pane to fix (Privacy & Security → Accessibility / Automation → Terminal).

## Wi-Fi ("which wifi am I on?", "what network am I connected to")

macOS 15 (Sequoia) REDACTS the Wi-Fi network name unless the terminal app has
Location Services permission — so `networksetup -getairportnetwork` and the old
`airport -I` wrongly report "not associated" even when connected. Use this instead:

```bash
DEV=$(networksetup -listallhardwareports | awk '/Wi-Fi/{getline; print $2}')
IP=$(ipconfig getifaddr "$DEV" 2>/dev/null)
SSID=$(ipconfig getsummary "$DEV" 2>/dev/null | awk -F 'SSID : ' '/ SSID : / {print $2; exit}')
echo "IP=$IP SSID=$SSID"
```

Interpret:
- No `IP` → genuinely NOT connected to Wi-Fi.
- `IP` present but `SSID` is empty or `<redacted>` → you ARE connected, but macOS is
  hiding the name. Tell the user: "You're connected to Wi-Fi, sir, but macOS won't
  reveal the network name until you grant Location Services permission to your terminal
  app in System Settings › Privacy & Security › Location Services."
- Otherwise report the actual SSID.
NEVER say "not connected" just because the SSID is hidden — check the IP first.

## Typing into search fields — clear first

Search boxes (Slack Cmd+K, browser bars, app search fields) often keep the PREVIOUS
query. Before typing a new search term, CLEAR the field so it doesn't append/concatenate:
select-all then delete, then type.

```applescript
keystroke "a" using {command down}   -- select existing text
delay 0.1
key code 51                          -- delete
delay 0.1
keystroke "your search term"
```

(The built-in slack_dm / slack_photo tools already do this.)

## Clicking in a browser (Chrome) — use click_web, never coordinates

To click ANY link/tab/button on a web page (ticket boards, dashboards, etc.), use the
`click_web` tool with the exact visible text (e.g. "Sprints"). It clicks the
element by text via JavaScript — precise. Do NOT use `mouse` (x,y) or vision to
click web pages; they hit the wrong element. If not found, `read_browser` first
to get the exact label, then click_web. (Requires Chrome › View › Developer ›
"Allow JavaScript from Apple Events".)

## Slack status

Use the `slack_status` tool. Reliable when SLACK_USER_TOKEN (xoxp- with
users.profile:write) is set — it calls the Slack API. Without a token it falls
back to UI automation (less reliable). Pass text, optional emoji (":coffee:"),
and minutes to auto-clear.

## Everyday primitives (prefer these over UI automation)

- **Files**: `find_files` (Spotlight/mdfind) finds by name/content/kind/recency — never hunt through Finder windows or `ls` blindly.
- **Text into apps**: for anything long or containing unicode/emoji, `type_text` automatically pastes via clipboard (fast, lossless). The user's clipboard is restored afterwards.
- **Clipboard**: `clipboard` action=read/write. "Copy that to my clipboard" → write.
- **Notifications**: `notify` shows a native banner — use it when finishing background work the user may not be watching.
- **Focus / Do Not Disturb**: there is no public CLI — the user should create Shortcuts named e.g. "Focus On" / "Focus Off" once; then `shortcut` action=run name="Focus On" toggles it. `shortcut` action=list shows what exists.
- **System switches**: `system_toggle` — dark_mode, wifi on/off, lock_screen, caffeinate (keep awake), empty_trash (confirms; unrecoverable).

## Comms

- **"Anything in my inbox?"** → `comms_summary` (Gmail tab + Slack API + Mail.app). Gmail leg needs an open mail.google.com tab and Chrome's View → Developer → Allow JavaScript from Apple Events. Slack leg needs SLACK_USER_TOKEN.
- **iMessage**: resolve the person with `contacts` first (one-time Contacts permission), then `imessage` with the phone/email handle. If 2+ contacts match, ask which one.

## Organizer (Calendar / Reminders / Notes)

- Resolve natural-language dates YOURSELF into ISO `YYYY-MM-DDTHH:MM` using the LIVE CONTEXT date ("tomorrow 3pm" → compute it) before calling `calendar`/`reminders`.
- "Remind me to X at 5" → `reminders` action=create (syncs to iPhone/Watch). NOT a background job.
- Notes bodies are plain text in, HTML inside — `notes` handles the conversion. Append ONLY to a note named explicitly.
- Fast calendar reads want icalBuddy: `brew install ical-buddy` (AppleScript fallback works but is slow on big calendars).
