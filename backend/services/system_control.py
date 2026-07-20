"""
macOS PC control for Cosmos.

Action hierarchy (most to least preferred):
  url_scheme > app_action > open_path > open_app > run_background > run_terminal

Never open Terminal to launch apps. Never use Terminal for searches.
"""

import asyncio
import os
import re
import shlex
import time
from pathlib import Path

# shlex.quote is the correct function (_q was removed in Python 3.12)
_q = shlex.quote

# ─── App aliases ──────────────────────────────────────────────────────────────

APP_ALIASES: dict[str, str] = {
    "vscode": "Visual Studio Code", "vs code": "Visual Studio Code",
    "visual studio code": "Visual Studio Code", "code editor": "Visual Studio Code",
    "cursor": "Cursor", "cursor editor": "Cursor",
    "xcode": "Xcode", "sublime": "Sublime Text",
    "chrome": "Google Chrome", "google chrome": "Google Chrome",
    "safari": "Safari", "firefox": "Firefox",
    "brave": "Brave Browser", "arc": "Arc",
    "terminal": "Terminal", "iterm": "iTerm2", "iterm2": "iTerm2", "warp": "Warp",
    "slack": "Slack", "teams": "Microsoft Teams",
    "zoom": "zoom.us", "notion": "Notion", "obsidian": "Obsidian",
    "spotify": "Spotify", "finder": "Finder",
    "app store": "App Store", "appstore": "App Store",
    "notes": "Notes", "calendar": "Calendar", "mail": "Mail",
    "word": "Microsoft Word", "excel": "Microsoft Excel",
    "powerpoint": "Microsoft PowerPoint", "pages": "Pages",
    "numbers": "Numbers", "keynote": "Keynote",
    "figma": "Figma", "postman": "Postman",
    "docker": "Docker Desktop",
    "system preferences": "System Preferences",
    "system settings": "System Settings",
    "activity monitor": "Activity Monitor",
    "calculator": "Calculator",
    "photo booth": "Photo Booth", "photobooth": "Photo Booth",
    "camera": "Photo Booth", "camera app": "Photo Booth",
    "facetime": "FaceTime", "face time": "FaceTime",
    "preview": "Preview", "quicktime": "QuickTime Player",
    "imovie": "iMovie", "garageband": "GarageBand",
    "xcode": "Xcode",
}

# URL schemes for deep-linking into apps
URL_SEARCH_SCHEMES: dict[str, str] = {
    "app store":    "macappstores://search?q={query}",
    "spotify":      "spotify:search:{query}",
    "maps":         "maps://?q={query}",
}

URL_OPEN_SCHEMES: dict[str, str] = {
    "app store":    "macappstores://",
    "system settings wifi":       "x-apple.systempreferences:com.apple.preference.network",
    "system settings bluetooth":  "x-apple.systempreferences:com.apple.BluetoothSettings",
    "system settings display":    "x-apple.systempreferences:com.apple.Displays-Settings.extension",
    "system settings sound":      "x-apple.systempreferences:com.apple.preference.sound",
}

WORK_DIR = Path.home() / "Desktop" / "cosmos-workspace"
WORK_DIR.mkdir(parents=True, exist_ok=True)

# Homebrew cask names for common apps
BREW_CASK_MAP: dict[str, str] = {
    "Sublime Text":          "sublime-text",
    "Visual Studio Code":    "visual-studio-code",
    "Google Chrome":         "google-chrome",
    "Firefox":               "firefox",
    "iTerm2":                "iterm2",
    "Warp":                  "warp",
    "Slack":                 "slack",
    "Zoom":                  "zoom",
    "Docker Desktop":        "docker",
    "Postman":               "postman",
    "Figma":                 "figma",
    "Obsidian":              "obsidian",
    "Notion":                "notion",
    "Arc":                   "arc",
    "Brave Browser":         "brave-browser",
    "Rectangle":             "rectangle",
    "Alfred":                "alfred",
    "1Password":             "1password",
    "Cursor":                "cursor",
    "TablePlus":             "tableplus",
    "Insomnia":              "insomnia",
}

# User identity — used when "myself", "me", "my" appears in commands
USER_NAME         = os.getenv("USER_NAME",         "Ravindra")
USER_FIRST_NAME   = os.getenv("USER_FIRST_NAME",   "Ravindra")
USER_EMAIL        = os.getenv("USER_EMAIL",        "")
USER_SLACK_HANDLE = os.getenv("USER_SLACK_HANDLE", "ravindra.c")


# ─── Low-level helpers ─────────────────────────────────────────────────────────

async def _run(cmd: list[str], input_text: str | None = None) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.PIPE if input_text else None,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdin_bytes = input_text.encode() if input_text else None
    stdout, stderr = await proc.communicate(stdin_bytes)
    return proc.returncode, stdout.decode(), stderr.decode()


PERMISSION_HINT = (
    "Needs Accessibility permission. "
    "Go to: System Settings → Privacy & Security → Accessibility → add Terminal. "
    "Also check: Privacy & Security → Automation → Terminal → enable System Events."
)


async def run_applescript(script: str, timeout: float = 30) -> tuple[bool, str]:
    """osascript with a hard ceiling — Calendar/Notes/Reminders can stall on
    big libraries, and an unbounded communicate() froze runs before the
    per-tool watchdog existed."""
    try:
        code, out, err = await asyncio.wait_for(
            _run(["osascript", "-e", script]), timeout)
    except asyncio.TimeoutError:
        return False, f"AppleScript timed out after {int(timeout)}s"
    result = (out or err).strip()
    if code != 0 and ("-1743" in result or "Not authorised" in result or "not allowed" in result.lower()):
        return False, PERMISSION_HINT
    return code == 0, result


async def _ensure_app(app: str, warm_s: float = 1.2) -> None:
    """Start `app` hidden (no focus steal) if it isn't running — AppleScript
    dictionaries error with -600 against a dead process."""
    if not await _is_app_running(app):
        await run_shell(f"open -a {_q(app)} -g -j", timeout=10)
        await asyncio.sleep(warm_s)


def _kill_proc_group(proc) -> None:
    """Kill the subprocess and everything it spawned (best effort)."""
    import signal
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.kill()
        except Exception:
            pass


# Login-shell environment, snapshotted ONCE — `bash -lc` re-sources the whole
# profile (~100-300ms) on every call otherwise. Falls back to -lc if the
# snapshot fails so PATH-dependent commands (brew, gh) always work.
_login_env: dict | None = None


async def _get_login_env() -> dict | None:
    global _login_env
    if _login_env is not None:
        return _login_env or None
    try:
        proc = await asyncio.create_subprocess_exec(
            "bash", "-lc", "env -0",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
        env: dict[str, str] = {}
        for pair in out.decode(errors="replace").split("\0"):
            if "=" in pair:
                k, v = pair.split("=", 1)
                env[k] = v
        _login_env = env if env.get("PATH") else {}
    except Exception:
        _login_env = {}
    return _login_env or None


def _truncate_head_tail(out: str, max_chars: int) -> str:
    """Keep the head AND the tail — build/test logs put the actual error at the
    END, and head-only truncation used to hide it (costing a whole extra LLM
    round-trip to re-run with a filter)."""
    if len(out) <= max_chars:
        return out
    head = int(max_chars * 0.55)
    tail = int(max_chars * 0.40)
    hidden = len(out) - head - tail
    return (out[:head]
            + f"\n…[truncated: {hidden} chars hidden — head and tail shown, of {len(out)} total]…\n"
            + out[-tail:])


async def run_shell(command: str, timeout: float | None = None,
                    max_chars: int = 20000) -> tuple[bool, str]:
    """Silent background shell. No Terminal window.

    - `timeout` (seconds): on expiry the WHOLE process group is killed (not just
      abandoned) and asyncio.TimeoutError is raised. Cancellation kills it too.
    - Output longer than `max_chars` keeps head AND tail with an explicit
      marker so a cut result is never mistaken for the full output.
    """
    env = await _get_login_env()
    if env:
        proc = await asyncio.create_subprocess_exec(
            "bash", "-c", command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
            env=env,
        )
    else:
        proc = await asyncio.create_subprocess_exec(
            "bash", "-lc", command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        _kill_proc_group(proc)
        try:
            await proc.wait()
        except Exception:
            pass
        raise
    except asyncio.CancelledError:
        _kill_proc_group(proc)
        raise
    # errors="replace": binary output (accidentally cat-ing a PDF/image) must
    # degrade to replacement chars, not crash the whole tool call.
    out = (stdout.decode(errors="replace") + stderr.decode(errors="replace")).strip()
    return proc.returncode == 0, _truncate_head_tail(out, max_chars)


# ─── Cached binary lookup (cliclick / imagesnap / ffmpeg …) ────────────────────
# shutil.which per call is cheap, but the old pattern spawned a whole
# `bash -lc "command -v x"` subprocess (~100ms+) on EVERY mouse click / photo.
# Misses are re-checked so a mid-session `brew install` is picked up.

_which_cache: dict[str, str] = {}
_EXTRA_BIN_DIRS = ("/opt/homebrew/bin", "/usr/local/bin")


def which_tool(name: str) -> str | None:
    import shutil as _shutil
    hit = _which_cache.get(name)
    if hit:
        return hit
    path = _shutil.which(name)
    if not path:
        for d in _EXTRA_BIN_DIRS:
            cand = os.path.join(d, name)
            if os.path.exists(cand):
                path = cand
                break
    if path:
        _which_cache[name] = path
    return path


_app_discovery_cache: dict[str, str] = {}


async def _discover_app(name: str) -> str:
    """
    Search /Applications and ~/Applications for the best match.
    Falls back to Spotlight (mdfind) for apps installed elsewhere.
    """
    import glob

    query = name.lower()
    # Check cache first
    if query in _app_discovery_cache:
        return _app_discovery_cache[query]

    # Search /Applications and ~/Applications
    search_dirs = ["/Applications", os.path.expanduser("~/Applications")]
    candidates: list[str] = []
    for d in search_dirs:
        if os.path.isdir(d):
            for app_path in glob.glob(f"{d}/*.app"):
                app_name = os.path.basename(app_path)[:-4]  # strip .app
                candidates.append(app_name)

    # Score candidates: exact match > starts-with > contains
    lower_candidates = {c.lower(): c for c in candidates}
    if query in lower_candidates:
        result = lower_candidates[query]
        _app_discovery_cache[query] = result
        return result
    for lc, c in lower_candidates.items():
        if lc.startswith(query) or query.startswith(lc):
            _app_discovery_cache[query] = c
            return c
    for lc, c in lower_candidates.items():
        if query in lc or lc in query:
            _app_discovery_cache[query] = c
            return c

    # Spotlight fallback (finds apps in subdirectories, other locations)
    ok, out = await run_shell(
        f"mdfind -name '{name}' -onlyin /Applications -onlyin ~/Applications 2>/dev/null | grep '\\.app$' | head -1"
    )
    if ok and out.strip():
        result = os.path.basename(out.strip())[:-4]
        _app_discovery_cache[query] = result
        return result

    # Nothing found — return title-cased name and let AppleScript try
    return query.title()


def _resolve_app(name: str) -> str:
    """
    Synchronous app name resolution.
    Uses the alias map first (fast), then falls back to title-casing.
    For full discovery, use _discover_app (async).
    """
    key = name.lower().strip()
    # Strip articles: "the photo booth" → "photo booth"
    for prefix in ("the ", "a ", "an "):
        if key.startswith(prefix):
            key = key[len(prefix):]
            break
    # Strip trailing "app": "photo booth app" → "photo booth"
    if key.endswith(" app"):
        key = key[:-4].strip()
    # Check alias map first
    if key in APP_ALIASES:
        return APP_ALIASES[key]
    # Check discovery cache (populated by previous async lookups)
    if key in _app_discovery_cache:
        return _app_discovery_cache[key]
    return key.title()


# ─── Actions ──────────────────────────────────────────────────────────────────

async def open_app(name: str) -> tuple[bool, str]:
    """
    Open app using 'open -a' command (gives proper system permissions incl. camera/mic)
    then bring to foreground via System Events. Invalidates the focus cache —
    the frontmost app is about to change.
    """
    invalidate_focus_cache()
    canonical = await _discover_app(name)
    if not canonical or canonical == name.title():
        canonical = _resolve_app(name)

    # Use 'open -a' — this launches with full user-level permissions
    # (camera, microphone, etc.) unlike AppleScript activate
    ok, msg = await run_shell(f"open -a {_q(canonical)}")
    if ok:
        # Return immediately — the frontmost nudge runs detached (~900ms of
        # sleep+activate ceremony off every open). Flows that type/click into
        # the app (type_text, click_ui, slack_message) do their own activation.
        async def _nudge():
            try:
                await asyncio.sleep(0.5)
                await run_applescript(
                    f'tell application "{canonical}" to activate\n'
                    f'tell application "System Events" to tell process "{canonical}" to set frontmost to true'
                )
            except Exception:
                pass
        asyncio.create_task(_nudge())
        return True, f"Opened {canonical}"

    # If 'open -a' fails, app might not be installed
    if "Unable to find application" in msg or "can't open" in msg.lower():
        cask = BREW_CASK_MAP.get(canonical)
        return False, f"__NOT_INSTALLED__:{canonical}:{cask or ''}"

    # Last resort: AppleScript activate
    ok2, msg2 = await run_applescript(f'tell application "{canonical}" to activate')
    return ok2, f"Opened {canonical}" if ok2 else f"Could not open {canonical}: {msg}"


async def open_path(path: str, app: str | None = None) -> tuple[bool, str]:
    """Open file/folder in an app. Tries multiple methods in order."""
    expanded = os.path.expanduser(path)
    # Ensure the path exists; if it doesn't, try the desktop and common locations
    if not os.path.exists(expanded):
        for prefix in [os.path.expanduser("~/Desktop/"), os.path.expanduser("~/")]:
            candidate = os.path.join(prefix, path.lstrip("~/"))
            if os.path.exists(candidate):
                expanded = candidate
                break

    if app:
        canonical = _resolve_app(app)
        is_vscode  = "Visual Studio Code" in canonical
        is_cursor  = "Cursor" in canonical

        if is_vscode or is_cursor:
            # Try known CLI paths in order — subprocess PATH often misses /usr/local/bin
            cli_candidates = [
                "code" if is_vscode else "cursor",
                "/usr/local/bin/code",
                "/usr/bin/code",
                "/opt/homebrew/bin/code",
                os.path.expanduser("~/.local/bin/code"),
                "/Applications/Visual Studio Code.app/Contents/Resources/app/bin/code",
            ]
            for cli in cli_candidates:
                ok, msg = await run_shell(f"{_q(cli)} {_q(expanded)} 2>/dev/null")
                if ok:
                    return True, f"Opened {expanded} in {canonical}"

            # CLI not found — use 'open -a' as reliable fallback
            # This opens VS Code AND passes the folder as argument
            ok, msg = await run_shell(f"open -a {_q(canonical)} {_q(expanded)}")
            return (True, f"Opened {expanded} in {canonical}") if ok else (False, msg)

        ok, msg = await run_shell(f"open -a {_q(canonical)} {_q(expanded)}")
        return (True, f"Opened {expanded} in {canonical}") if ok else (False, msg)

    ok, msg = await run_shell(f"open {_q(expanded)}")
    return ok, f"Opened {expanded}"


# ─── Music / media control ─────────────────────────────────────────────────────

async def _running_player() -> str | None:
    """Which music player is running — Spotify wins over Music.app."""
    ok, out = await run_applescript(
        'tell application "System Events" to set procs to name of every process\n'
        'if procs contains "Spotify" then return "Spotify"\n'
        'if procs contains "Music" then return "Music"\n'
        'return ""'
    )
    name = (out or "").strip()
    return name if ok and name in ("Spotify", "Music") else None


async def media_control(action: str, player: str | None = None,
                        level: int | None = None) -> tuple[bool, str]:
    """Control Spotify / Apple Music without the LLM.

    action: play | pause | playpause | next | previous | now_playing | volume
    """
    app = player or await _running_player()
    if not app:
        return False, "No music player (Spotify or Music) is running."
    act = (action or "").lower().strip()

    if act == "now_playing":
        ok, out = await run_applescript(
            f'tell application "{app}"\n'
            '  if player state is playing then\n'
            '    return (name of current track) & " — " & (artist of current track)\n'
            '  else\n'
            '    return ""\n'
            '  end if\n'
            'end tell'
        )
        track = (out or "").strip()
        if not ok:
            return False, out
        return True, f"Now playing on {app}: {track}" if track else "Nothing is playing."

    if act == "volume":
        lvl = max(0, min(int(level or 50), 100))
        ok, out = await run_applescript(f'tell application "{app}" to set sound volume to {lvl}')
        return (True, f"{app} volume set to {lvl}%.") if ok else (False, out)

    cmd = {
        "play": "play", "resume": "play", "pause": "pause",
        "playpause": "playpause", "toggle": "playpause",
        "next": "next track", "skip": "next track",
        "previous": "previous track", "prev": "previous track", "back": "previous track",
    }.get(act)
    if not cmd:
        return False, f"Unknown media action: {action}"
    ok, out = await run_applescript(f'tell application "{app}" to {cmd}')
    return (True, f"{act} → {app}.") if ok else (False, out)


async def open_url(url: str, browser: str = "default") -> tuple[bool, str]:
    invalidate_focus_cache()      # the frontmost app/tab is about to change
    if not url.startswith("http"):
        url = "https://" + url
    if browser == "default":
        ok, _ = await run_shell(f"open {_q(url)}")
    else:
        canonical = _resolve_app(browser)
        ok, _ = await run_shell(f"open -a {_q(canonical)} {_q(url)}")
    return ok, f"Opened {url}"


async def open_url_scheme(url: str) -> tuple[bool, str]:
    """Deep-link via URL scheme (App Store, Spotify, Maps, etc.)."""
    ok, msg = await run_shell(f"open {_q(url)}")
    return ok, f"Launched: {url}"


async def app_action(app: str, action: str, value: str = "") -> tuple[bool, str]:
    """
    App-specific actions using AppleScript dictionaries.
    Actions: navigate, new_tab, search, back, forward, close_tab
    """
    canonical = await _discover_app(app)
    if not canonical or canonical == app.title():
        canonical = _resolve_app(app)
    lower = canonical.lower()

    scripts: dict[tuple[str, str], str] = {
        # Notes/Reminders/Calendar live in the dedicated notes()/reminders()/
        # calendar_*() functions now — the old entries here appended to
        # whatever note happened to be last modified.
        # Photo Booth — needs the File-menu polling shell pipeline (see skills/macos-control.md),
        # not a static AppleScript template. The agent runs it via bash.
        ("calendar", "open"):   'tell application "Calendar" to activate',
        # Safari
        ("safari", "navigate"):  f'tell application "Safari"\n    activate\n    open location "{_esc(value)}"\nend tell',
        ("safari", "new_tab"):   'tell application "Safari" to activate\ntell application "System Events"\n    keystroke "t" using {command down}\nend tell',
        ("safari", "search"):    f'tell application "Safari"\n    activate\n    open location "https://www.google.com/search?q={_url_encode(value)}"\nend tell',
        # Google Chrome
        ("google chrome", "navigate"): f'tell application "Google Chrome"\n    activate\n    open location "{_esc(value)}"\nend tell',
        ("google chrome", "new_tab"):  'tell application "Google Chrome" to activate\ntell application "System Events"\n    keystroke "t" using {{command down}}\nend tell',
        ("google chrome", "search"):   f'tell application "Google Chrome"\n    activate\n    open location "https://www.google.com/search?q={_url_encode(value)}"\nend tell',
        # Gmail send — Cmd+Enter is the universal Gmail send shortcut
        ("google chrome", "gmail_send"): '''tell application "Google Chrome" to activate
delay 0.3
tell application "System Events"
    tell process "Google Chrome"
        set frontmost to true
        delay 0.2
        keystroke return using {command down}
    end tell
end tell''',
        # Finder
        ("finder", "navigate"):  f'tell application "Finder"\n    activate\n    open (POSIX file "{_esc(os.path.expanduser(value))}") as alias\nend tell',
        # Mail
        ("mail", "compose"):     f'tell application "Mail"\n    activate\n    set m to make new outgoing message\n    set subject of m to "{_esc(value)}"\n    set visible of m to true\nend tell',
        # Terminal
        ("terminal", "run"):     f'tell application "Terminal"\n    activate\n    do script "{_esc(value)}"\nend tell',
        ("iterm2", "run"):       f'tell application "iTerm2"\n    activate\n    tell current window to create tab with default profile\n    tell current session of current window to write text "{_esc(value)}"\nend tell',
    }

    key = (lower, action.lower())
    script = scripts.get(key)

    # Generic fallbacks
    if not script and action == "navigate" and value.startswith("http"):
        script = f'tell application "{canonical}"\n    activate\n    open location "{_esc(value)}"\nend tell'
    if not script and action == "navigate" and not value.startswith("http"):
        script = f'tell application "Finder"\n    activate\n    open (POSIX file "{_esc(os.path.expanduser(value))}") as alias\nend tell'

    if not script:
        return False, f"No script for {canonical}/{action}"

    ok, msg = await run_applescript(script)
    label = {"navigate": f"Navigated to {value}", "new_tab": "Opened new tab",
             "search": f"Searched for {value}", "run": f"Running: {value}"}.get(action, action)
    return ok, label if ok else msg


async def ui_search(app: str, query: str) -> tuple[bool, str]:
    """
    Type a search query into an app.
    Strategy 1: URL scheme (most reliable, zero UI automation)
    Strategy 2: Cmd+F / search shortcut + keystroke
    """
    canonical = _resolve_app(app)
    app_key = canonical.lower()

    # Try URL scheme first — no UI automation needed
    if "app store" in app_key:
        ok, msg = await open_url_scheme(f"macappstores://search?q={_url_encode(query)}")
        return ok, f"Searched App Store for '{query}'"

    if "spotify" in app_key:
        ok, msg = await open_url_scheme(f"spotify:search:{_url_encode(query)}")
        return ok, f"Searched Spotify for '{query}'"

    if "maps" in app_key:
        ok, msg = await open_url_scheme(f"maps://?q={_url_encode(query)}")
        return ok, f"Searched Maps for '{query}'"

    if "safari" in app_key or "chrome" in app_key or "firefox" in app_key:
        # Just do a Google search
        await app_action(app, "search", query)
        return True, f"Searched for '{query}' in {canonical}"

    # Generic: full accessibility-based search with keyboard takeover
    return await focus_and_type_search(app, query)


async def type_text(text: str, app: str | None = None) -> tuple[bool, str]:
    """
    Type arbitrary text using System Events keyboard takeover.
    Activates the app first (or uses frontmost if no app given).
    Handles multi-line text, special chars, and long strings.
    """
    # Long or non-ASCII text → clipboard paste: System Events keystroke
    # mangles unicode/emoji and crawls on long strings.
    if len(text) > 80 or any(ord(c) > 126 for c in text):
        return await paste_text(text, app)
    if app:
        canonical = _resolve_app(app)
        await run_applescript(f'tell application "{canonical}" to activate')
        await asyncio.sleep(0.08)

    escaped = _esc(text)
    script = f'''
tell application "System Events"
    keystroke "{escaped}"
end tell
'''
    ok, msg = await run_applescript(script.strip())
    return ok, f"Typed text{' in ' + _resolve_app(app) if app else ''}"


async def click_ui_element(app: str, element_name: str, element_type: str = "button") -> tuple[bool, str]:
    """
    Find and click a UI element by name inside an app using Accessibility API.
    element_type: "button", "text field", "menu item", "checkbox", etc.
    """
    canonical = _resolve_app(app)
    esc_name  = _esc(element_name)
    script = f'''
tell application "{canonical}" to activate
delay 0.045
tell application "System Events"
    tell process "{canonical}"
        set frontmost to true
        delay 0.04
        try
            click {element_type} "{esc_name}" of window 1
            return "clicked"
        on error
            try
                set allElems to every {element_type} of window 1
                repeat with elem in allElems
                    if name of elem contains "{esc_name}" then
                        click elem
                        return "clicked"
                    end if
                end repeat
            end try
            return "not found"
        end try
    end tell
end tell
'''
    ok, result = await run_applescript(script.strip())
    if ok and "clicked" in result:
        return True, f"Clicked '{element_name}' in {canonical}"
    return False, f"Could not find '{element_name}' in {canonical}"


async def focus_and_type_search(app: str, query: str) -> tuple[bool, str]:
    """
    Full keyboard takeover for search: activate app, find search field via
    Accessibility API, click it, clear it, type query, press Enter.
    Works on any app with an accessible text field.
    """
    canonical = _resolve_app(app)
    escaped   = _esc(query)
    script = f'''
tell application "{canonical}" to activate
delay 0.08
tell application "System Events"
    tell process "{canonical}"
        set frontmost to true
        delay 0.04
        -- Try common search field patterns
        set foundField to false

        -- Pattern 1: text field with "search" in description/placeholder
        try
            set fields to every text field of window 1
            repeat with f in fields
                set desc to ""
                try
                    set desc to description of f
                end try
                try
                    if desc is "" then set desc to value of attribute "AXPlaceholderValue" of f
                end try
                if desc contains "earch" or desc contains "earch" then
                    click f
                    delay 0.08
                    keystroke "a" using {{command down}}
                    delay 0.04
                    keystroke "{escaped}"
                    delay 0.08
                    key code 36
                    set foundField to true
                    exit repeat
                end if
            end repeat
        end try

        -- Pattern 2: first text field in toolbar
        if not foundField then
            try
                set toolbars to every toolbar of window 1
                repeat with tb in toolbars
                    set fields to every text field of tb
                    if length of fields > 0 then
                        click (item 1 of fields)
                        delay 0.08
                        keystroke "a" using {{command down}}
                        delay 0.04
                        keystroke "{escaped}"
                        delay 0.08
                        key code 36
                        set foundField to true
                        exit repeat
                    end if
                end repeat
            end try
        end if

        -- Pattern 3: Cmd+F fallback (universal search shortcut)
        if not foundField then
            keystroke "f" using {{command down}}
            delay 0.045
            keystroke "{escaped}"
            delay 0.08
            key code 36
            set foundField to true
        end if

        return foundField as string
    end tell
end tell
'''
    ok, result = await run_applescript(script.strip())
    return ok, f"Searched '{query}' in {canonical}"


async def slack_message(recipient: str, message: str) -> tuple[bool, str]:
    """
    Send a Slack DM using the Cmd+K Quick Switcher.
    recipient: name/handle to search for. Use USER_FIRST_NAME for "myself"/"me".
    message:   exact text to send — never modified.
    """
    # Resolve "myself" / "me" to the actual user name
    resolved = recipient.strip()
    if resolved.lower() in ("myself", "me", "my", "i"):
        resolved = USER_FIRST_NAME

    esc_recipient = _esc(resolved)

    # Put the message on the clipboard and PASTE it — typing it with `keystroke`
    # turns every newline into a Return, which Slack sends, so a multi-line
    # message arrives as N stacked messages. Paste keeps it as ONE message.
    prev_clip = (await _run(["pbpaste"]))[1]
    await _run(["pbcopy"], input_text=message)

    script = f'''
tell application "Slack" to activate
delay 0.4
tell application "System Events"
    -- Open Quick Switcher
    keystroke "k" using {{command down}}
    delay 0.5
    -- Clear any leftover text in the switcher (select-all → delete) so the
    -- recipient isn't appended to a stale query.
    keystroke "a" using {{command down}}
    delay 0.1
    key code 51
    delay 0.15
    -- Type recipient name
    keystroke "{esc_recipient}"
    delay 0.6
    -- Select first result
    key code 36
    delay 0.7
    -- Paste the message as ONE block (newlines preserved, not sent per-line)
    keystroke "v" using {{command down}}
    delay 0.3
    -- Send once
    key code 36
end tell
'''
    ok, msg = await run_applescript(script.strip())
    # Restore whatever the user had on the clipboard.
    await _run(["pbcopy"], input_text=prev_clip)
    label = f"DM to {'yourself' if resolved == USER_FIRST_NAME else resolved}"
    return ok, f"Sent via Slack {label}: \"{message}\"" if ok else msg


async def capture_photo(path: str) -> tuple[bool, str]:
    """Capture ONE fresh frame from the webcam straight to `path` via imagesnap.

    This is the reliable way to take a photo — no Photo Booth, no hunting for a
    file in a library bundle. `-w 2` warms the camera 2s so the frame isn't
    black. Returns (True, absolute_path) on success.
    """
    path = os.path.expanduser(path)
    # Remove a stale file at this path first, so a failed capture can never be
    # mistaken for a fresh photo (this is exactly the bug we're fixing).
    try:
        os.remove(path)
    except OSError:
        pass

    if not which_tool("imagesnap"):
        return False, ("__NO_IMAGESNAP__ imagesnap isn't installed — it's the reliable "
                       "way to capture the webcam. Install it: brew install imagesnap")

    ok, out = await run_shell(f"imagesnap -w 2 {_q(path)}", timeout=25)
    if "Camera access not granted" in (out or ""):
        return False, ("__NO_CAMERA_PERM__ Camera access not granted. Grant Camera "
                       "permission to your terminal app in System Settings › Privacy & "
                       "Security › Camera, then retry.")
    if not os.path.exists(path) or os.path.getsize(path) < 1000:
        return False, f"Camera capture failed — {out.strip() or 'no image written'}"
    return True, path


_IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".gif", ".heic", ".webp", ".bmp", ".tiff")


async def slack_send_image(recipient: str, image_path: str,
                           caption: str = "") -> tuple[bool, str]:
    """Send an image OR video FILE to a Slack DM (slack_message only sends text).

    Images go on the clipboard as picture data; other files (video, etc.) go on
    as a file reference. Then: open the DM via Cmd+K, paste (Cmd+V), optional
    caption, send. The exact `image_path` is used — no guessing.
    """
    image_path = os.path.expanduser(image_path)
    if not os.path.exists(image_path):
        return False, f"File not found: {image_path}"

    resolved = recipient.strip()
    if resolved.lower() in ("myself", "me", "my", "i"):
        resolved = USER_FIRST_NAME

    # 1. Put the file on the clipboard — images as picture data (pastes inline),
    #    everything else as a file reference (Slack uploads it on paste).
    is_image = os.path.splitext(image_path)[1].lower() in _IMAGE_EXTS
    if is_image:
        copy_expr = f'set the clipboard to (read (POSIX file "{image_path}") as JPEG picture)'
    else:
        copy_expr = f'set the clipboard to (POSIX file "{image_path}")'
    copy_ok, copy_msg = await run_applescript(copy_expr)
    if not copy_ok:
        return False, f"Couldn't copy the file to the clipboard: {copy_msg}"

    # 2. Open the DM, paste, caption, send. Generous delays — the upload
    #    preview needs a beat to attach before Enter will send it.
    esc_recipient = _esc(resolved)
    caption_line = (f'keystroke "{_esc(caption)}"\n    delay 0.4\n    ' if caption else "")
    script = f'''
tell application "Slack" to activate
delay 0.5
tell application "System Events"
    keystroke "k" using {{command down}}
    delay 0.6
    keystroke "a" using {{command down}}
    delay 0.1
    key code 51
    delay 0.15
    keystroke "{esc_recipient}"
    delay 0.9
    key code 36
    delay 1.0
    keystroke "v" using {{command down}}
    delay 1.4
    {caption_line}key code 36
end tell
'''
    ok, msg = await run_applescript(script.strip())
    who = "yourself" if resolved == USER_FIRST_NAME else resolved
    kind = "photo" if is_image else "file"
    return (ok, f"Sent the {kind} to {who} on Slack." if ok else msg)


_HUD_URL_HINTS = ("localhost:5173", "127.0.0.1:5173")
_CHROMIUM_BROWSERS = ["Google Chrome", "Brave Browser", "Microsoft Edge",
                      "Arc", "Vivaldi", "Chromium"]


def _pick_av_devices(stderr: str) -> tuple[str | None, str | None]:
    """Parse `ffmpeg -f avfoundation -list_devices` output and pick the real
    webcam + built-in mic indices (avoiding Desk View, screen-capture, and
    virtual audio devices like ZoomAudioDevice)."""
    vid_idx = aud_idx = None
    section = None
    for line in stderr.splitlines():
        if "video devices:" in line:
            section = "v"; continue
        if "audio devices:" in line:
            section = "a"; continue
        m = re.search(r"\[(\d+)\]\s+(.+?)\s*$", line)
        if not m or section is None:
            continue
        idx, name = m.group(1), m.group(2).lower()
        if section == "v" and vid_idx is None:
            if "camera" in name and "desk view" not in name and "capture screen" not in name:
                vid_idx = idx
        elif section == "a" and aud_idx is None:
            if ("microphone" in name or "built-in" in name) and "zoom" not in name:
                aud_idx = idx
    return vid_idx, aud_idx


async def record_video(path: str, duration: int = 5) -> tuple[bool, str]:
    """Record `duration` seconds of webcam video (with mic audio) to `path` via
    ffmpeg + avfoundation. Auto-detects the real camera/mic so it works across
    machines. Returns (True, absolute_path) on success."""
    path = os.path.expanduser(path)
    duration = max(1, min(int(duration or 5), 120))
    try:
        os.remove(path)
    except OSError:
        pass

    if not which_tool("ffmpeg"):
        return False, ("__NO_FFMPEG__ ffmpeg isn't installed — required to record video. "
                       "Install it: brew install ffmpeg")

    # CRITICAL: ffmpeg's avfoundation camera-open HANGS forever when camera
    # permission isn't granted (no error, just blocks). imagesnap fails in ~1s
    # instead, so probe with it first to return a clean error, never a hang.
    if which_tool("imagesnap"):
        _probe = os.path.expanduser("~/.friday/.camprobe.jpg")
        try:
            os.makedirs(os.path.dirname(_probe), exist_ok=True)
            _, pout = await run_shell(f"imagesnap -w 1 {_q(_probe)}", timeout=10)
            try: os.remove(_probe)
            except OSError: pass
            if "Camera access not granted" in (pout or ""):
                return False, ("__NO_CAMERA_PERM__ Camera access not granted. Grant Camera "
                               "(and Microphone) permission to your terminal app in System "
                               "Settings › Privacy & Security, then retry.")
        except asyncio.TimeoutError:
            pass  # probe stalled — fall through; the recording timeout will bound it

    # Enumerate devices (ffmpeg prints them to stderr and exits non-zero).
    try:
        _, derr = await asyncio.wait_for(
            (await asyncio.create_subprocess_exec(
                "ffmpeg", "-f", "avfoundation", "-list_devices", "true", "-i", "",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)).communicate(),
            timeout=10)
        vid, aud = _pick_av_devices(derr.decode("utf-8", "replace"))
    except Exception:
        vid, aud = None, None
    if vid is None:
        vid = "0"  # sane default: first video device
    device = f"{vid}:{aud}" if aud is not None else vid

    out_cmd = (f'ffmpeg -y -f avfoundation -framerate 30 -i {_q(device)} '
               f'-t {duration} -pix_fmt yuv420p -c:v libx264 -preset ultrafast '
               f'-c:a aac {_q(path)}')
    try:
        ok, out = await run_shell(out_cmd, timeout=duration + 25)
    except asyncio.TimeoutError:
        return False, ("Video recording timed out — the camera may be blocked or in use "
                       "by another app. Check Camera permission and close other camera apps.")
    low = (out or "").lower()
    if "not been given permission" in low or "camera access" in low or "access denied" in low:
        return False, ("__NO_CAMERA_PERM__ Camera access not granted. Grant Camera (and "
                       "Microphone) permission to your terminal app in System Settings › "
                       "Privacy & Security, then retry.")
    if not os.path.exists(path) or os.path.getsize(path) < 2000:
        return False, f"Video capture failed — {(out or '').strip()[-300:] or 'no file written'}"
    return True, path


# Which browser actually hosts the HUD — cached after the first successful
# refocus so later calls try ONE browser instead of probing up to 7.
_hud_browser: str | None = None


async def focus_friday_window() -> tuple[bool, str]:
    """Bring the browser tab running the COSMOS HUD (localhost:5173) back to the
    front after a task activated other apps. Best-effort: searches each running
    browser for the tab and, if found, activates that window/tab. No-op (returns
    False) if the HUD tab can't be located."""
    invalidate_focus_cache()      # the frontmost app is about to change
    global _hud_browser
    url_cond = " or ".join(f'(tabUrl contains "{h}")' for h in _HUD_URL_HINTS)

    # Chromium-family browsers share the same AppleScript tab model.
    # Last known winner first; probe the rest only if it misses.
    browsers = list(_CHROMIUM_BROWSERS)
    if _hud_browser in browsers:
        browsers.remove(_hud_browser)
        browsers.insert(0, _hud_browser)
    for app in browsers:
        script = f'''
if application "{app}" is running then
  tell application "{app}"
    repeat with w in windows
      set idx to 0
      repeat with t in tabs of w
        set idx to idx + 1
        set tabUrl to (URL of t)
        if {url_cond} then
          set active tab index of w to idx
          set index of w to 1
          activate
          return "focused"
        end if
      end repeat
    end repeat
  end tell
end if
return "notfound"'''
        ok, out = await run_applescript(script.strip())
        if ok and "focused" in (out or ""):
            _hud_browser = app
            return True, f"Returned focus to COSMOS ({app})"

    # Safari uses a slightly different tab API.
    safari = f'''
if application "Safari" is running then
  tell application "Safari"
    repeat with w in windows
      repeat with t in tabs of w
        set tabUrl to (URL of t)
        if {url_cond} then
          set current tab of w to t
          set index of w to 1
          activate
          return "focused"
        end if
      end repeat
    end repeat
  end tell
end if
return "notfound"'''
    ok, out = await run_applescript(safari.strip())
    if ok and "focused" in (out or ""):
        _hud_browser = "Safari"
        return True, "Returned focus to COSMOS (Safari)"
    _hud_browser = None
    return False, "COSMOS HUD window not found in any browser"


async def send_keystroke(keys: str, app: str | None = None) -> tuple[bool, str]:
    """
    Send a keyboard shortcut. Format: "cmd+t", "cmd+shift+n", "return", "escape"
    """
    parts = [p.strip().lower() for p in keys.split("+")]
    modifiers = []
    key = parts[-1]

    mod_map = {"cmd": "command down", "ctrl": "control down",
               "shift": "shift down", "opt": "option down", "alt": "option down"}
    key_code_map = {"return": "key code 36", "enter": "key code 36",
                    "tab": "key code 48", "space": "key code 49",
                    "delete": "key code 51", "escape": "key code 53",
                    "up": "key code 126", "down": "key code 125",
                    "left": "key code 123", "right": "key code 124",
                    "f5": "key code 96", "f12": "key code 111"}

    for p in parts[:-1]:
        if p in mod_map:
            modifiers.append(mod_map[p])

    if app:
        canonical = _resolve_app(app)
        activate = f'tell application "{canonical}" to activate\ndelay 0.04\n'
    else:
        activate = ""

    if key in key_code_map:
        ks = key_code_map[key]
        if modifiers:
            ks = ks.replace("key code", "key code") + f' using {{{", ".join(modifiers)}}}'
    else:
        mod_str = f' using {{{", ".join(modifiers)}}}' if modifiers else ""
        ks = f'keystroke "{key}"{mod_str}'

    script = f'{activate}tell application "System Events"\n    {ks}\nend tell'
    ok, msg = await run_applescript(script.strip())
    return ok, f"Sent {keys}"


async def write_and_open_file(filename: str, content: str, app: str | None = None) -> tuple[bool, str, str]:
    path = (WORK_DIR / filename).resolve()
    try:
        path.relative_to(WORK_DIR.resolve())
    except ValueError:
        return False, "", f"Path escapes workspace: {filename}"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")

    if app:
        canonical = _resolve_app(app)
        if "Visual Studio Code" in canonical or "Cursor" in canonical:
            cli = "code" if "Visual Studio Code" in canonical else "cursor"
            ok, _ = await run_shell(f"{cli} {_q(str(path))}")
            if not ok:
                await run_shell(f"open -a {_q(canonical)} {_q(str(path))}")
        else:
            await run_shell(f"open -a {_q(canonical)} {_q(str(path))}")
    else:
        await run_shell(f"open {_q(str(path))}")

    return True, str(path), content


async def run_in_terminal(command: str) -> tuple[bool, str]:
    """Always opens a NEW Terminal window. Only for explicit terminal requests."""
    escaped = _esc(command)
    script = f'tell application "Terminal"\n    activate\n    do script "{escaped}"\nend tell'
    ok, msg = await run_applescript(script)
    return ok, f"Running in Terminal: {command}"


async def run_applescript_direct(script: str) -> tuple[bool, str]:
    """
    Execute raw AppleScript. Used for anything not covered by named actions.
    This is the universal fallback — full keyboard, mouse, UI, app control.
    """
    ok, result = await run_applescript(script)
    return ok, result[:500] if result else ("Success" if ok else "AppleScript error")


async def universal_action(description: str, applescript: str) -> tuple[bool, str]:
    """
    Execute any macOS action via raw AppleScript.
    The LLM writes the script; this executes it.
    Used when no named action covers the request.

    On success returns the script's ACTUAL result (agents use this tool to read
    data back — e.g. window names); falls back to the description only when the
    script produced no output.
    """
    ok, result = await run_applescript(applescript)
    if ok:
        result = result.strip()
        return True, result[:2000] if result else description
    return False, f"Failed: {result[:200]}"


async def _get_browser_url(app: str = "Google Chrome") -> str | None:
    """Get the URL of the active tab in Chrome or Safari."""
    canonical = _resolve_app(app)
    if "Chrome" in canonical:
        script = f'tell application "{canonical}" to get URL of active tab of front window'
    else:
        script = f'tell application "Safari" to get URL of front document'
    ok, result = await run_applescript(script)
    return result.strip() if ok and result.strip().startswith("http") else None


async def read_browser_page(
    app: str = "Google Chrome",
    selector: str | None = None,
    max_chars: int = 8000,
) -> tuple[bool, str]:
    """
    Read text content from the active browser tab.
    Strategy 1: Get current URL → fetch with httpx (no JS permission needed).
    Strategy 2: JavaScript execution (requires View > Developer > Allow JS from Apple Events).
    """
    from services import web_search as _ws

    # Strategy 1: get URL and fetch it directly (always works)
    url = await _get_browser_url(app)
    if url and url.startswith("http"):
        text = await _ws.fetch_page_text(url, max_chars=max_chars)
        if text and len(text) > 100:
            return True, text

    # Strategy 2: JavaScript execution (needs Chrome setting enabled)
    canonical = _resolve_app(app)
    js = f"document.body.innerText.slice(0, {max_chars})"
    js_escaped = js.replace('"', '\\"')
    if "Chrome" in canonical:
        script = f'tell application "{canonical}" to execute front window\'s active tab javascript "{js_escaped}"'
    else:
        script = f'tell application "Safari" to do JavaScript "{js_escaped}" in front document'
    ok, result = await run_applescript(script)
    if ok and len(result) > 50:
        return True, result[:max_chars]

    return False, "Could not read browser content. Try opening the page in Chrome first."


async def run_browser_js(
    javascript: str,
    app: str = "Google Chrome",
) -> tuple[bool, str]:
    """
    Execute JavaScript OR fetch current page and parse it.
    Falls back to URL fetch if JS execution is disabled.
    """
    # Try JS execution first
    canonical = _resolve_app(app)
    js_escaped = javascript.replace('"', '\\"').replace("\n", "\\n")
    if "Chrome" in canonical:
        script = f'tell application "{canonical}" to execute front window\'s active tab javascript "{js_escaped}"'
    else:
        script = f'tell application "Safari" to do JavaScript "{js_escaped}" in front document'
    ok, result = await run_applescript(script)
    if ok:
        return True, result[:6000]

    # Fallback: fetch URL and return raw text
    from services import web_search as _ws
    url = await _get_browser_url(app)
    if url:
        text = await _ws.fetch_page_text(url)
        if text:
            return True, f"[fetched page text — JS unavailable]\n{text[:6000]}"
    return False, result


SLACK_USER_TOKEN = os.getenv("SLACK_USER_TOKEN", "").strip()


async def _slack_api(method: str, json_payload: str | None = None,
                     timeout: float = 15) -> dict:
    """One Slack Web-API call with the user token OFF the argv. Returns the
    parsed JSON ({} on any failure)."""
    import json as _json
    from services.curlutil import SecretFiles
    sf = SecretFiles()
    try:
        args = ["curl", "-s", "-X", "POST", f"https://slack.com/api/{method}",
                *sf.header(f"Authorization: Bearer {SLACK_USER_TOKEN}")]
        if json_payload is not None:
            args += ["-H", "Content-Type: application/json; charset=utf-8",
                     *sf.data(json_payload)]
        proc = await asyncio.create_subprocess_exec(
            *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
        return _json.loads(out.decode() or "{}")
    except Exception as e:
        return {"ok": False, "error": str(e)[:120]}
    finally:
        sf.cleanup()


async def set_slack_status(text: str, emoji: str = "", minutes: int = 0) -> tuple[bool, str]:
    """Set your Slack status. Uses the Slack API (robust) when SLACK_USER_TOKEN
    is set; otherwise falls back to a best-effort UI flow. `emoji` like ':coffee:',
    `minutes` = auto-clear after N minutes (0 = don't clear)."""
    import json as _json, time as _time
    emoji = emoji.strip()
    if emoji and not emoji.startswith(":"):
        emoji = f":{emoji.strip(':')}:"

    if SLACK_USER_TOKEN:
        exp = int(_time.time()) + minutes * 60 if minutes else 0
        payload = _json.dumps({"profile": {
            "status_text": text, "status_emoji": emoji, "status_expiration": exp}})
        data = await _slack_api("users.profile.set", payload)
        if data.get("ok"):
            return True, f'Slack status set to "{text}"' + (f" {emoji}" if emoji else "")
        return False, f"Slack API error: {data.get('error', 'unknown')}"

    # UI fallback — open the status editor via the avatar (top-right), type, save.
    esc = _esc(text)
    script = f'''
tell application "Slack" to activate
delay 0.6
tell application "System Events" to tell process "Slack"
    set p to position of front window
    set s to size of front window
    set avX to (item 1 of p) + (item 1 of s) - 22
    set avY to (item 2 of p) + 46
end tell
do shell script "/usr/bin/env cliclick c:" & (avX as integer) & "," & (avY as integer)
delay 0.7
tell application "System Events"
    keystroke "{esc}"
    delay 0.3
    key code 36
end tell
'''
    ok, msg = await run_applescript(script.strip())
    if ok:
        return True, (f'Attempted to set Slack status to "{text}" via the UI. '
                      "For reliable status setting, add a SLACK_USER_TOKEN (see skill).")
    return False, msg


async def gmail_attach(path: str) -> tuple[bool, str]:
    """Attach a file to the open Gmail compose window. Finds the 'Attach files'
    paperclip via JS, computes its ON-SCREEN position, does a REAL mouse click
    (a programmatic JS click is blocked by Chrome for file dialogs), then
    navigates the native Open dialog to the file."""
    p = os.path.expanduser(path)
    if not os.path.exists(p):
        return False, f"File not found: {p}"

    # Find the paperclip and return its screen-space center. Page→screen:
    # x = screenX + rect.centerX ; y = screenY + topChrome + rect.centerY,
    # where topChrome = outerHeight - innerHeight (tab/tool/bookmark bars).
    js = ("(function(){"
          "var E=[].slice.call(document.querySelectorAll('[aria-label],[data-tooltip]'));"
          "var m=E.find(function(e){var l=((e.getAttribute('aria-label')||e.getAttribute('data-tooltip')||'')+'').toLowerCase();"
          "var r=e.getBoundingClientRect();return l.indexOf('attach file')>-1&&r.width>1&&r.height>1;});"
          "if(!m)return 'NOATTACH';m.scrollIntoView({block:'center'});var r=m.getBoundingClientRect();"
          "var x=Math.round(window.screenX+r.left+r.width/2);"
          "var y=Math.round(window.screenY+(window.outerHeight-window.innerHeight)+r.top+r.height/2);"
          "return x+','+y;})()")
    ok, res = await run_browser_js(js, "Google Chrome")
    res = (res or "").strip()
    if not ok or "NOATTACH" in res or "," not in res:
        return False, ("Couldn't find Gmail's Attach paperclip — make sure a compose "
                       "window is open and focused in Chrome.")
    try:
        x, y = (int(v) for v in res.split(",")[:2])
    except Exception:
        return False, f"Couldn't parse the attach button position ({res})."

    await mouse_control("click", x, y)      # REAL click → native Open dialog
    await asyncio.sleep(1.3)
    ok2, msg = await choose_file_in_dialog(p)
    return (ok2, f"Attached {os.path.basename(p)} to the Gmail draft." if ok2 else msg)


async def choose_file_in_dialog(path: str) -> tuple[bool, str]:
    """When a native macOS Open/Choose-file dialog is showing, navigate to `path`
    and open it (Cmd+Shift+G → type path → Return → Return). Use for Gmail/Slack
    attach buttons, upload dialogs, etc. The dialog must already be open."""
    p = os.path.expanduser(path)
    if not os.path.exists(p):
        return False, f"File not found: {p}"
    esc = _esc(p)
    script = f'''
tell application "System Events"
    keystroke "g" using {{command down, shift down}}
    delay 0.5
    keystroke "{esc}"
    delay 0.4
    key code 36
    delay 0.6
    key code 36
end tell
'''
    ok, msg = await run_applescript(script.strip())
    return (ok, f"Selected {p} in the file dialog." if ok else msg)


async def browser_click_text(text: str, app: str = "Google Chrome") -> tuple[bool, str]:
    """Click a web element by its VISIBLE TEXT (via JS) — precise, no coordinate
    guessing. Prefers interactive elements (links, buttons, tabs, menu items),
    exact match first, then prefix/substring. Returns (ok, message).

    Requires Chrome's 'Allow JavaScript from Apple Events' (View › Developer)."""
    import base64 as _b64
    q64 = _b64.b64encode(text.encode("utf-8")).decode()
    # Single-quotes only (run_browser_js escapes double-quotes for AppleScript).
    # Finds the SMALLEST element whose text matches, then clicks its nearest
    # clickable ancestor — so sidebar/nav items rendered as <div><svg/><span>…
    # (where the span holds the text but the parent is the link) still work.
    js = (
        "(function(){"
        "var q=decodeURIComponent(escape(atob('" + q64 + "'))).trim().toLowerCase();"
        "var nz=function(s){return (s||'').replace(/\\s+/g,' ').trim().toLowerCase();};"
        "var t=function(e){return nz(e.getAttribute&&(e.getAttribute('aria-label')||e.getAttribute('title'))||e.innerText||e.textContent);};"
        "var vis=function(e){var r=e.getBoundingClientRect();var s=getComputedStyle(e);return r.width>1&&r.height>1&&s.visibility!=='hidden'&&s.display!=='none';};"
        "var clk=function(e){while(e&&e!==document.body){var g=e.tagName;if(g==='A'||g==='BUTTON'||g==='SUMMARY')return e;var r=e.getAttribute&&e.getAttribute('role');if(r&&/^(button|tab|link|menuitem|option)$/.test(r))return e;if(e.hasAttribute&&(e.hasAttribute('onclick')||e.getAttribute('tabindex')!==null))return e;try{if(getComputedStyle(e).cursor==='pointer')return e;}catch(_){}e=e.parentElement;}return null;};"
        "var A=[].slice.call(document.querySelectorAll('a,button,[role],[onclick],[tabindex],li,span,div,p')).filter(vis);"
        "var ex=[],pf=[],sb=[];"
        "for(var i=0;i<A.length;i++){var e=A[i];var tx=t(e);if(!tx)continue;if(tx===q)ex.push(e);else if(tx.indexOf(q)===0&&tx.length<q.length+30)pf.push(e);else if(tx.indexOf(q)>-1&&e.children.length<=3&&tx.length<q.length+40)sb.push(e);}"
        "var c=ex.concat(pf).concat(sb)[0];"
        "if(!c)return 'NOTFOUND';"
        "var m=clk(c)||c;"
        "m.scrollIntoView({block:'center',inline:'center'});"
        "['pointerover','pointerdown','mousedown','pointerup','mouseup','click'].forEach(function(ev){m.dispatchEvent(new MouseEvent(ev,{bubbles:true,cancelable:true,view:window}));});"
        "return 'CLICKED:'+t(m).slice(0,80);"
        "})()"
    )
    ok, res = await run_browser_js(js, app)
    r = (res or "").strip()
    if not ok:
        return False, res
    if r.startswith("[fetched page text"):
        return False, ("Can't run JavaScript in the browser — enable Chrome › View › "
                       "Developer › 'Allow JavaScript from Apple Events', then retry.")
    if "NOTFOUND" in r:
        return False, (f"No clickable element matching '{text}' on the page. "
                       "Read the page first (read_browser) to see the exact label.")
    return True, r.replace("CLICKED:", f"Clicked '{text}' → matched: ")


async def read_app_text(
    app: str,
    max_chars: int = 6000,
) -> tuple[bool, str]:
    """
    Read visible text from a native macOS app using the Accessibility API.
    No screenshot needed — reads text elements directly from the UI tree.
    Works for Slack, Mail, Notes, any native app.
    """
    canonical = _resolve_app(app)

    # Use a shell command with osascript to avoid blocking issues
    import shlex as _shlex
    safe_app = canonical.replace('"', '\\"')
    script = f'''
tell application "{safe_app}" to activate
delay 0.5
tell application "System Events"
    tell process "{safe_app}"
        set allText to ""
        -- Try flat static text first
        try
            repeat with t in (every static text of window 1)
                set tv to value of t as string
                if length of tv > 2 then set allText to allText & tv & linefeed
            end repeat
        end try
        -- Try nested in groups
        if length of allText < 10 then
            try
                repeat with g in (every group of window 1)
                    repeat with t in (every static text of g)
                        set tv to value of t as string
                        if length of tv > 2 then set allText to allText & tv & linefeed
                    end repeat
                end repeat
            end try
        end if
        -- Try scroll areas (Slack messages live here)
        if length of allText < 10 then
            try
                repeat with sa in (every scroll area of window 1)
                    repeat with t in (every static text of sa)
                        set tv to value of t as string
                        if length of tv > 2 then set allText to allText & tv & linefeed
                    end repeat
                    repeat with g in (every group of sa)
                        repeat with t in (every static text of g)
                            set tv to value of t as string
                            if length of tv > 2 then set allText to allText & tv & linefeed
                        end repeat
                    end repeat
                end repeat
            end try
        end if
        return allText
    end tell
end tell
'''
    ok, result = await run_applescript(script.strip())
    if ok and len(result.strip()) > 10:
        return True, result[:max_chars]
    return False, f"Could not read text from {canonical} via accessibility API"


async def read_file(path: str, max_chars: int = 8000) -> tuple[bool, str]:
    """Read a file and return its contents (up to max_chars)."""
    expanded = os.path.expanduser(path)
    ok, content = await run_shell(f"cat {_q(expanded)} 2>&1 | head -c {max_chars}")
    if not ok or not content.strip():
        # Try as directory listing
        ok2, listing = await run_shell(f"ls -la {_q(expanded)} 2>&1")
        if ok2:
            return True, listing[:max_chars]
    return ok, content if ok else f"Cannot read {path}: {content}"


async def get_focus_context() -> str:
    """One-line snapshot of what the user is looking at RIGHT NOW: frontmost
    app, front window title, and (for browsers) the active tab URL. Bounded to
    ~2s and returns '' on any failure — this feeds the volatile prompt tail and
    must never delay or break a run."""
    try:
        ok, out = await asyncio.wait_for(run_applescript(
            'tell application "System Events"\n'
            '  set p to first process whose frontmost is true\n'
            '  set appName to name of p\n'
            '  set winTitle to ""\n'
            '  try\n'
            '    set winTitle to name of front window of p\n'
            '  end try\n'
            'end tell\n'
            'return appName & "|||" & winTitle'), timeout=2.0)
        if not ok or "|||" not in (out or ""):
            return ""
        app, _, title = out.partition("|||")
        app, title = app.strip(), title.strip()
        parts = [app]
        if title:
            parts.append(f'"{title}"')
        if app in _CHROMIUM_BROWSERS or app == "Safari":
            try:
                url = await asyncio.wait_for(_get_browser_url(app), timeout=2.0)
                if url:
                    parts.append(url[:150])
            except Exception:
                pass
        return " — ".join(parts)
    except Exception:
        return ""


# Focus-context cache: the probe above costs ~440ms of serial AppleScript on
# every non-read run. A voice command prefetches it during STT's ~0.5-1s
# endpointing dead time (the HUD's 'prefetch' frame); the run then reads it
# here for free. Short TTL — "what's frontmost" goes stale fast.
_FOCUS_TTL_S = 4.0
_focus_cache: tuple[float, str] | None = None
_focus_inflight: "asyncio.Task[str] | None" = None


async def get_focus_context_cached() -> str:
    """get_focus_context with a short cache + in-flight dedup, so a prefetch
    and the run it precedes share ONE probe. Same contract: '' on failure."""
    global _focus_cache, _focus_inflight
    if _focus_cache and time.monotonic() - _focus_cache[0] < _FOCUS_TTL_S:
        return _focus_cache[1]
    if _focus_inflight is None or _focus_inflight.done():
        _focus_inflight = asyncio.create_task(get_focus_context())
    try:
        # shield: a cancelled caller (fast path won the race) must not kill
        # the probe other callers are waiting on.
        value = await asyncio.shield(_focus_inflight)
    except Exception:
        return ""
    # Never cache a failed probe — "" would suppress retries for the TTL.
    if value:
        _focus_cache = (time.monotonic(), value)
    return value


def invalidate_focus_cache() -> None:
    """Call after any action that changes the frontmost app — a stale cached
    focus within the TTL would misresolve 'this'/'here' for the next run."""
    global _focus_cache
    _focus_cache = None


async def get_system_state() -> dict:
    """Snapshot of current system state: frontmost app, open apps, disk,
    battery, Wi-Fi, current time. (The old server-cwd git fields were dropped —
    they described COSMOS's backend dir, not anything the user asked about.)"""
    results = await asyncio.gather(
        run_shell("osascript -e 'tell application \"System Events\" to get name of every process whose background only is false'"),
        run_shell("df -h / | tail -1 | awk '{print $4\" free of \"$2}'"),
        run_shell("date '+%A %B %d, %H:%M'"),
        run_shell("pmset -g batt | grep -Eo '[0-9]+%.*' | head -1"),
        run_shell("networksetup -getairportnetwork en0 2>/dev/null | sed 's/^.*: //'"),
        get_focus_context(),
        return_exceptions=True,
    )

    def _val(r, fallback="unknown"):
        if isinstance(r, BaseException):
            return fallback
        ok, out = r
        return out.strip() if ok and out else fallback

    apps_r = results[0]
    open_apps = []
    if not isinstance(apps_r, BaseException) and apps_r[0]:
        open_apps = [a.strip() for a in apps_r[1].split(",")][:12]
    focus = results[5] if isinstance(results[5], str) else ""
    return {
        "frontmost":    focus or "unknown",
        "open_apps":    open_apps,
        "disk_free":    _val(results[1]),
        "current_time": _val(results[2]),
        "battery":      _val(results[3], "n/a (desktop?)"),
        "wifi":         _val(results[4], "off/unknown"),
    }



# ─── Personal organizer: Calendar / Reminders / Notes ──────────────────────────

def _applescript_date(var: str, iso: str) -> str:
    """Build an AppleScript date COMPONENT-WISE — date-string literals are
    locale-dependent and break on non-US formats. iso: 'YYYY-MM-DDTHH:MM'."""
    from datetime import datetime as _dt
    d = _dt.fromisoformat(iso)
    return (f"set {var} to current date\n"
            f"set year of {var} to {d.year}\n"
            f"set month of {var} to {d.month}\n"
            f"set day of {var} to {d.day}\n"
            f"set hours of {var} to {d.hour}\n"
            f"set minutes of {var} to {d.minute}\n"
            f"set seconds of {var} to 0")


async def _run_organizer_script(script: str, app: str,
                                timeout: float = 30) -> tuple[bool, str]:
    """run_applescript with one relaunch-retry: Calendar/Reminders/Notes drop
    their AppleEvent connection when idle (-609) or dead (-600)."""
    ok, out = await run_applescript(script, timeout)
    if not ok and ("-609" in (out or "") or "-600" in (out or "")):
        await run_shell(f"open -a {_q(app)} -g -j", timeout=10)
        await asyncio.sleep(1.5)
        ok, out = await run_applescript(script, timeout)
    return ok, out


async def calendar_events(scope: str = "today") -> tuple[bool, str]:
    """List events: scope = today | tomorrow | week. Prefers icalBuddy
    (fast, all accounts); falls back to scripting Calendar.app."""
    scope = (scope or "today").lower().strip()
    if which_tool("icalBuddy"):
        flag = {"today": "eventsToday", "tomorrow": 'eventsFrom:tomorrow to:tomorrow',
                "week": "eventsToday+7"}.get(scope, "eventsToday")
        try:
            # attendees included: meeting prep needs WHO, not just when/where.
            ok, out = await run_shell(
                f'icalBuddy -nc -b "- " -iep "title,datetime,location,attendees" '
                f'-po "datetime,title,location,attendees" {flag}', timeout=15)
            if ok:
                return True, out.strip() or f"No events for {scope}."
        except asyncio.TimeoutError:
            pass
    # AppleScript fallback — bounded window, can be slow on huge calendars.
    days = {"today": 1, "tomorrow": 2, "week": 7}.get(scope, 1)
    offset = 1 if scope == "tomorrow" else 0
    await _ensure_app("Calendar", warm_s=2.0)
    script = f'''
set d1 to current date
set time of d1 to 0
set d1 to d1 + ({offset} * days)
set d2 to d1 + ({days - offset} * days)
set out to ""
tell application "Calendar"
  repeat with cal in calendars
    try
      repeat with e in (every event of cal whose start date ≥ d1 and start date < d2)
        set out to out & (start date of e as string) & " — " & (summary of e) & linefeed
      end repeat
    end try
  end repeat
end tell
return out'''
    ok, out = await _run_organizer_script(script.strip(), "Calendar", timeout=45)
    if not ok:
        return False, (f"{out} — for fast calendar reads: brew install ical-buddy")
    return True, (out or "").strip() or f"No events for {scope}."


async def calendar_create(title: str, start_iso: str, duration_min: int = 30,
                          calendar_name: str = "", location: str = "",
                          notes_text: str = "") -> tuple[bool, str]:
    try:
        date_block = _applescript_date("startDate", start_iso)
    except Exception:
        return False, f"Bad start time '{start_iso}' — use YYYY-MM-DDTHH:MM."
    await _ensure_app("Calendar", warm_s=2.0)
    target = (f'calendar "{_esc(calendar_name)}"' if calendar_name
              else "first calendar whose writable is true")
    props = f'summary:"{_esc(title)}", start date:startDate, end date:endDate'
    if location:
        props += f', location:"{_esc(location)}"'
    if notes_text:
        props += f', description:"{_esc(notes_text)}"'
    script = f'''
{date_block}
set endDate to startDate + ({max(5, int(duration_min or 30))} * minutes)
tell application "Calendar"
  set targetCal to {target}
  tell targetCal to make new event with properties {{{props}}}
  return "created in " & (name of targetCal)
end tell'''
    ok, out = await _run_organizer_script(script.strip(), "Calendar", timeout=30)
    if ok:
        return True, f'Event "{title}" {out.strip() or "created"} at {start_iso}.'
    # Older Calendar versions lack `writable` — retry on calendar 1.
    if not calendar_name and "writable" in (out or ""):
        script = script.replace("first calendar whose writable is true", "calendar 1")
        ok, out = await _run_organizer_script(script.strip(), "Calendar", timeout=30)
        if ok:
            return True, f'Event "{title}" created at {start_iso}.'
    return False, out


async def reminders(action: str, name: str = "", due_iso: str = "",
                    list_name: str = "") -> tuple[bool, str]:
    """action = create | list_due | complete. Native Reminders sync to the
    user's iPhone/Watch for free."""
    act = (action or "").lower().strip()
    await _ensure_app("Reminders", warm_s=1.5)

    if act == "create":
        if not name:
            return False, "Reminder needs a name."
        target = f'list "{_esc(list_name)}"' if list_name else "default list"
        due_block, due_props = "", ""
        if due_iso:
            try:
                due_block = _applescript_date("dueDate", due_iso)
                due_props = ", due date:dueDate, remind me date:dueDate"
            except Exception:
                return False, f"Bad due time '{due_iso}' — use YYYY-MM-DDTHH:MM."
        script = f'''
{due_block}
tell application "Reminders"
  tell {target}
    make new reminder with properties {{name:"{_esc(name)}"{due_props}}}
  end tell
end tell
return "ok"'''
        ok, out = await _run_organizer_script(script.strip(), "Reminders", timeout=20)
        when = f" for {due_iso}" if due_iso else ""
        return (True, f'Reminder "{name}" created{when}.') if ok else (False, out)

    if act == "list_due":
        scope = f'list "{_esc(list_name)}"' if list_name else "default list"
        script = f'''
set out to ""
set n to 0
tell application "Reminders"
  repeat with r in (reminders of {scope} whose completed is false)
    set n to n + 1
    if n > 15 then exit repeat
    set dueTxt to ""
    try
      if due date of r is not missing value then
        set dueTxt to " — due " & (due date of r as string)
      end if
    end try
    set out to out & "- " & (name of r) & dueTxt & linefeed
  end repeat
end tell
return out'''
        ok, out = await _run_organizer_script(script.strip(), "Reminders", timeout=30)
        if not ok:
            return False, out
        return True, (out or "").strip() or "No open reminders."

    if act == "complete":
        if not name:
            return False, "Which reminder? Give (part of) its name."
        script = f'''
tell application "Reminders"
  set r to first reminder whose name contains "{_esc(name)}" and completed is false
  set completed of r to true
  return name of r
end tell'''
        ok, out = await _run_organizer_script(script.strip(), "Reminders", timeout=20)
        return (True, f'Completed "{out.strip()}".') if ok else (False, out)

    return False, f"Unknown reminders action '{action}' — use create|list_due|complete."


def _html_esc(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def _strip_html(s: str) -> str:
    s = re.sub(r"<br[^>]*>|</div>|</p>", "\n", s, flags=re.IGNORECASE)
    s = re.sub(r"<[^>]+>", "", s)
    return s.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">") \
            .replace("&nbsp;", " ").strip()


async def notes(action: str, title: str = "", body: str = "",
                query: str = "") -> tuple[bool, str]:
    """Apple Notes: action = create | search | read | append. Bodies are HTML
    inside Notes — plain text is escaped and wrapped per line."""
    act = (action or "").lower().strip()
    await _ensure_app("Notes", warm_s=1.5)

    if act == "create":
        if not title:
            return False, "Note needs a title."
        html = "".join(f"<div>{_html_esc(line) or '<br>'}</div>"
                       for line in (body or "").split("\n"))
        script = f'''
tell application "Notes"
  make new note with properties {{name:"{_esc(title)}", body:"{_esc(f"<div><h1>{_html_esc(title)}</h1></div>" + html)}"}}
end tell
return "ok"'''
        ok, out = await _run_organizer_script(script.strip(), "Notes", timeout=20)
        return (True, f'Note "{title}" created.') if ok else (False, out)

    if act == "search":
        q = query or title
        script = f'''
set out to ""
set n to 0
tell application "Notes"
  repeat with nt in (every note whose name contains "{_esc(q)}")
    set n to n + 1
    if n > 15 then exit repeat
    set out to out & "- " & (name of nt) & "  (modified " & (modification date of nt as string) & ")" & linefeed
  end repeat
end tell
return out'''
        ok, out = await _run_organizer_script(script.strip(), "Notes", timeout=30)
        if not ok:
            return False, out
        return True, (out or "").strip() or f"No notes matching '{q}'."

    if act == "read":
        script = f'''
tell application "Notes"
  set nt to first note whose name contains "{_esc(title or query)}"
  return body of nt
end tell'''
        ok, out = await _run_organizer_script(script.strip(), "Notes", timeout=20)
        return (True, _strip_html(out)[:8000]) if ok else (False, out)

    if act == "append":
        if not title:
            return False, "Append needs the note's title — never a bare 'latest note'."
        html = "".join(f"<div>{_html_esc(line) or '<br>'}</div>"
                       for line in (body or "").split("\n"))
        script = f'''
tell application "Notes"
  set nt to first note whose name contains "{_esc(title)}"
  set body of nt to (body of nt) & "{_esc(html)}"
  return name of nt
end tell'''
        ok, out = await _run_organizer_script(script.strip(), "Notes", timeout=20)
        return (True, f'Appended to "{out.strip()}".') if ok else (False, out)

    return False, f"Unknown notes action '{action}' — use create|search|read|append."


# ─── Comms awareness: inbox triage + iMessage ──────────────────────────────────

async def _browser_js_in_tab(url_substring: str, javascript: str,
                             app: str = "Google Chrome") -> tuple[bool, str]:
    """Execute JS in the first tab whose URL contains `url_substring` — the
    tab does NOT need to be active. Requires Chrome's View→Developer→
    'Allow JavaScript from Apple Events'."""
    esc_js = _esc(javascript)
    script = f'''
if application "{app}" is running then
  tell application "{app}"
    repeat with w in windows
      repeat with t in tabs of w
        if (URL of t) contains "{url_substring}" then
          return execute t javascript "{esc_js}"
        end if
      end repeat
    end repeat
  end tell
end if
return "__NO_TAB__"'''
    ok, out = await run_applescript(script.strip())
    if ok and "__NO_TAB__" in (out or ""):
        return False, f"no open tab matching {url_substring}"
    return ok, out


async def _is_app_running(app: str) -> bool:
    ok, out = await run_applescript(
        f'tell application "System Events" to (name of processes) contains "{app}"')
    return ok and "true" in (out or "").lower()


async def comms_summary() -> tuple[bool, str]:
    """'Anything important in my inbox?' — Gmail tab + Slack unreads + Mail.app.
    Each leg degrades independently: whatever is unavailable says why instead
    of failing the whole summary."""
    lines: list[str] = []

    # ── Gmail (via the open browser tab — no API/OAuth needed) ──
    gmail_js = (
        "(() => { const rows=[...document.querySelectorAll('tr.zA.zE')];"
        "const items=rows.slice(0,5).map(r=>{"
        "const f=r.querySelector('.yW span')?.innerText||'?';"
        "const s=r.querySelector('.y6')?.innerText||'?';"
        "return f+': '+s.slice(0,80)});"
        "return JSON.stringify({unread:rows.length,items}) })()")
    ok, out = await _browser_js_in_tab("mail.google.com", gmail_js)
    if ok:
        try:
            import json as _json
            g = _json.loads(out)
            lines.append(f"Gmail: {g.get('unread', 0)} unread on screen"
                         + ("".join(f"\n  - {i}" for i in g.get("items", []))))
        except Exception:
            lines.append("Gmail: tab found but couldn't parse the inbox "
                         "(enable View → Developer → Allow JavaScript from Apple Events in Chrome).")
    else:
        lines.append("Gmail: no open mail.google.com tab to read.")

    # ── Slack (Web API — users.counts gives unread totals) ──
    if SLACK_USER_TOKEN:
        data = await _slack_api("users.counts")
        if data.get("ok"):
            ims = [i for i in data.get("ims", []) if i.get("dm_count", 0) > 0]
            ch  = [c for c in data.get("channels", []) if c.get("unread_count_display", 0) > 0]
            total = sum(i.get("dm_count", 0) for i in ims) + \
                    sum(c.get("unread_count_display", 0) for c in ch)
            lines.append(f"Slack: {total} unread across {len(ims)} DMs and {len(ch)} channels.")
        else:
            lines.append(f"Slack: API error ({data.get('error', 'unknown')}).")
    else:
        lines.append("Slack: no SLACK_USER_TOKEN configured — can't read unreads.")

    # ── Mail.app (only if it's already running — never launch it) ──
    if await _is_app_running("Mail"):
        ok, out = await asyncio.wait_for(
            run_applescript('tell application "Mail" to get unread count of inbox'),
            timeout=5)
        if ok and (out or "").strip().isdigit():
            lines.append(f"Mail.app: {out.strip()} unread.")

    return True, "\n".join(lines)


async def contacts_lookup(name: str) -> tuple[bool, str]:
    """Find people in Contacts.app by name — returns name / phones / emails
    lines (max 5). First use triggers a one-time Contacts permission prompt."""
    esc = _esc(name.strip())
    await _ensure_app("Contacts")
    script = f'''
tell application "Contacts"
  set out to ""
  set n to 0
  repeat with p in (every person whose name contains "{esc}")
    set n to n + 1
    if n > 5 then exit repeat
    set phoneList to ""
    repeat with ph in phones of p
      set phoneList to phoneList & (value of ph) & " "
    end repeat
    set emailList to ""
    repeat with em in emails of p
      set emailList to emailList & (value of em) & " "
    end repeat
    set out to out & (name of p) & " | phones: " & phoneList & "| emails: " & emailList & linefeed
  end repeat
end tell
return out'''
    try:
        ok, out = await asyncio.wait_for(run_applescript(script.strip()), timeout=15)
    except asyncio.TimeoutError:
        return False, "Contacts lookup timed out (grant Contacts permission?)."
    if not ok:
        return False, out
    out = (out or "").strip()
    return (True, out) if out else (True, f"No contacts matching '{name}'.")


async def send_imessage(recipient: str, message: str) -> tuple[bool, str]:
    """Send an iMessage via Messages.app. `recipient` is a phone/email handle
    (use contacts_lookup to resolve names first)."""
    handle = recipient.strip()
    # Strip phone formatting — Messages wants bare digits (keep leading +).
    if re.match(r"^[+\d][\d\s\-().]+$", handle):
        handle = re.sub(r"[^\d+]", "", handle)
    esc_h, esc_m = _esc(handle), _esc(message)
    script = f'''
tell application "Messages"
  set targetService to 1st account whose service type = iMessage
  try
    send "{esc_m}" to participant "{esc_h}" of targetService
    return "sent"
  on error
    send "{esc_m}" to buddy "{esc_h}" of targetService
    return "sent"
  end try
end tell'''
    try:
        ok, out = await asyncio.wait_for(run_applescript(script.strip()), timeout=15)
    except asyncio.TimeoutError:
        return False, "Messages.app didn't respond in 15s."
    if ok and "sent" in (out or ""):
        # Journal for promise-mining; an iMessage cannot be unsent → undoable=False.
        from services import outbox
        outbox.record("imessage", "send", target=recipient,
                      summary=message[:200], undoable=False)
        return True, f"iMessage sent to {recipient}."
    return False, out or "Messages could not send (is the handle iMessage-capable?)."


# ─── Everyday macOS primitives ─────────────────────────────────────────────────

async def get_clipboard() -> tuple[bool, str]:
    code, out, err = await _run(["pbpaste"])
    if code != 0:
        return False, err.strip() or "pbpaste failed"
    return True, out[:8000] if out else "(clipboard is empty)"


async def set_clipboard(text: str) -> tuple[bool, str]:
    code, _, err = await _run(["pbcopy"], input_text=text)
    return (True, f"Copied {len(text)} chars to clipboard.") if code == 0 \
        else (False, err.strip() or "pbcopy failed")


async def paste_text(text: str, app: str | None = None) -> tuple[bool, str]:
    """Type via clipboard + Cmd+V — instant and unicode/emoji-safe (System
    Events `keystroke` mangles non-ASCII and crawls on long strings). The
    previous clipboard is restored afterwards."""
    _, prev, _ = await _run(["pbpaste"])
    code, _, err = await _run(["pbcopy"], input_text=text)
    if code != 0:
        return False, f"clipboard write failed: {err.strip()}"
    if app:
        canonical = _resolve_app(app)
        await run_applescript(f'tell application "{canonical}" to activate')
        await asyncio.sleep(0.15)
    ok, msg = await send_keystroke("cmd+v", None)
    await asyncio.sleep(0.15)
    await _run(["pbcopy"], input_text=prev)   # restore what the user had
    return (ok, f"Pasted {len(text)} chars{' in ' + _resolve_app(app) if app else ''}") \
        if ok else (False, msg)


_MDFIND_KINDS = {
    "pdf":          'kMDItemContentType == "com.adobe.pdf"',
    "image":        'kMDItemContentTypeTree == "public.image"',
    "video":        'kMDItemContentTypeTree == "public.movie"',
    "audio":        'kMDItemContentTypeTree == "public.audio"',
    "folder":       'kMDItemContentType == "public.folder"',
    "presentation": 'kMDItemContentTypeTree == "public.presentation"',
    "spreadsheet":  'kMDItemDisplayName == "*.xlsx"cd || kMDItemDisplayName == "*.csv"cd || kMDItemDisplayName == "*.numbers"cd',
    "document":     'kMDItemContentTypeTree == "public.content"',
}


async def find_files(name: str = "", content: str = "", kind: str = "",
                     within_days: int = 0, onlyin: str = "",
                     limit: int = 20) -> tuple[bool, str]:
    """Spotlight (mdfind) file search — answers "where's that CSV from
    last week" with data instead of blind GUI driving."""
    q: list[str] = []
    if name:
        q.append(f'kMDItemDisplayName == "*{name}*"cd')
    if content:
        q.append(f'kMDItemTextContent == "*{content}*"cd')
    k = _MDFIND_KINDS.get((kind or "").lower().strip())
    if k:
        q.append(f"({k})")
    if within_days and int(within_days) > 0:
        q.append(f"kMDItemFSContentChangeDate >= $time.today(-{int(within_days)})")
    if not q:
        return False, "Give at least one of: name, content, kind."
    scope = os.path.expanduser(onlyin or "~")
    limit = max(1, min(int(limit or 20), 50))
    cmd = (f"mdfind -onlyin {_q(scope)} {_q(' && '.join(q))} 2>/dev/null "
           f"| head -{limit}")
    try:
        ok, out = await run_shell(cmd, timeout=10)
    except asyncio.TimeoutError:
        return False, "Spotlight search timed out."
    paths = [p for p in (out or "").splitlines() if p.strip()]
    if not paths:
        return True, "No files matched."
    # ONE stat for all hits (argv exec form, no shell) — the old per-path
    # run_shell loop cost a subprocess each, ~150-450ms per search.
    try:
        proc = await asyncio.create_subprocess_exec(
            "stat", "-f", "%N\t%z bytes · %Sm", "-t", "%Y-%m-%d %H:%M", *paths,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        out_b, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
        info: dict[str, str] = {}
        for line in (out_b or b"").decode(errors="replace").splitlines():
            p, _, meta = line.partition("\t")
            if meta.strip():
                info[p] = meta.strip()
        detailed = [f"{p}  ({info[p]})" if p in info else p for p in paths]
    except Exception:
        detailed = list(paths)
    return True, "\n".join(detailed)


async def list_shortcuts() -> tuple[bool, str]:
    try:
        ok, out = await run_shell("shortcuts list", timeout=15)
    except asyncio.TimeoutError:
        return False, "shortcuts CLI timed out."
    return ok, (out.strip() or "(no Shortcuts found)") if ok else out


async def run_shortcut(name: str, input_text: str = "") -> tuple[bool, str]:
    """Run a user Shortcut by name — unlocks Focus/DND toggles and any personal
    automation the user has built in Shortcuts.app."""
    cmd = f"shortcuts run {_q(name)}"
    if input_text:
        cmd = f"echo {_q(input_text)} | {cmd}"
    try:
        ok, out = await run_shell(cmd, timeout=60)
    except asyncio.TimeoutError:
        return False, f"Shortcut '{name}' timed out after 60s."
    return (True, out.strip() or f"Shortcut '{name}' ran.") if ok else (False, out)


async def notify(title: str, message: str, sound: bool = True) -> tuple[bool, str]:
    """Native macOS notification — reaches the user even when the HUD tab is
    closed (and is the scheduler's delivery channel)."""
    script = f'display notification "{_esc(message[:230])}" with title "{_esc(title[:60])}"'
    if sound:
        script += ' sound name "Glass"'
    ok, out = await run_applescript(script)
    return (True, "Notification shown.") if ok else (False, out)


_caffeinate_proc: asyncio.subprocess.Process | None = None


async def system_toggle(feature: str, state: str = "toggle") -> tuple[bool, str]:
    """Small system switches: dark_mode | wifi | lock_screen | caffeinate |
    empty_trash. state: on | off | toggle (where meaningful)."""
    global _caffeinate_proc
    f = (feature or "").lower().strip()
    s = (state or "toggle").lower().strip()

    if f == "dark_mode":
        value = {"on": "true", "off": "false"}.get(s, "not dark mode")
        ok, out = await run_applescript(
            'tell application "System Events" to tell appearance preferences '
            f"to set dark mode to {value}")
        return (True, f"Dark mode {s}.") if ok else (False, out)

    if f == "wifi":
        if s not in ("on", "off"):
            return False, "wifi needs state on|off."
        ok, out = await run_shell(f"networksetup -setairportpower en0 {s}", timeout=10)
        return (True, f"Wi-Fi {s}.") if ok else (False, out)

    if f == "lock_screen":
        ok, out = await run_applescript(
            'tell application "System Events" to keystroke "q" '
            "using {control down, command down}")
        return (True, "Screen locked.") if ok else (False, out)

    if f == "caffeinate":
        if s == "on":
            if _caffeinate_proc and _caffeinate_proc.returncode is None:
                return True, "Already keeping the Mac awake."
            _caffeinate_proc = await asyncio.create_subprocess_exec(
                "caffeinate", "-dims",
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
            return True, "Keeping the Mac awake (caffeinate running)."
        if _caffeinate_proc and _caffeinate_proc.returncode is None:
            _caffeinate_proc.kill()
            _caffeinate_proc = None
            return True, "Sleep re-enabled."
        return True, "Caffeinate wasn't running."

    if f == "empty_trash":
        ok, out = await run_applescript(
            'tell application "Finder" to empty trash')
        return (True, "Trash emptied.") if ok else (False, out)

    return False, f"Unknown toggle '{feature}' — use dark_mode|wifi|lock_screen|caffeinate|empty_trash."


async def take_screenshot(save_path: str | None = None) -> tuple[bool, str]:
    path = save_path or str(WORK_DIR / "screenshot.png")
    ok, _ = await run_shell(f"screencapture -x {_q(path)}")
    return ok, path


async def mouse_control(action: str, x: int = 0, y: int = 0, button: str = "left") -> tuple[bool, str]:
    """
    Mouse/touchpad control.
    action: move | click | double_click | right_click | scroll_up | scroll_down
    Uses cliclick if available (brew install cliclick), falls back to AppleScript.
    """
    if which_tool("cliclick"):
        cmd_map = {
            "move":         f"cliclick m:{x},{y}",
            "click":        f"cliclick c:{x},{y}",
            "double_click": f"cliclick dc:{x},{y}",
            "right_click":  f"cliclick rc:{x},{y}",
            "scroll_up":    f"cliclick ku:{x},{y}",
            "scroll_down":  f"cliclick kd:{x},{y}",
        }
        cmd = cmd_map.get(action, f"cliclick m:{x},{y}")
        ok, msg = await run_shell(cmd)
        return ok, f"Mouse {action} at ({x},{y})"

    # AppleScript fallback (requires Accessibility permission)
    as_map = {
        "move":         f'tell application "System Events" to set mouse location to {{{x}, {y}}}',
        "click":        f'tell application "System Events" to click at {{{x}, {y}}}',
        "double_click": f'tell application "System Events" to double click at {{{x}, {y}}}',
        "right_click":  f'tell application "System Events" to right click at {{{x}, {y}}}',
        "scroll_up":    f'tell application "System Events" to scroll up at {{{x}, {y}}}',
        "scroll_down":  f'tell application "System Events" to scroll down at {{{x}, {y}}}',
    }
    script = as_map.get(action, as_map["click"])
    ok, msg = await run_applescript(script)
    return ok, f"Mouse {action} at ({x},{y})" if ok else msg


async def get_cursor_position() -> tuple[int, int]:
    """Return current mouse cursor position."""
    ok, out = await run_shell(
        "python3 -c \"import Quartz; p = Quartz.CGEventGetLocation(Quartz.CGEventCreate(None)); print(int(p.x), int(p.y))\""
    )
    if ok and out:
        parts = out.strip().split()
        if len(parts) == 2:
            return int(parts[0]), int(parts[1])
    return 0, 0


async def set_volume(level: int) -> tuple[bool, str]:
    level = max(0, min(100, level))
    ok, _ = await run_applescript(f"set volume output volume {level}")
    return ok, f"Volume {level}%"


async def adjust_volume(delta: int) -> tuple[bool, str]:
    """Relative volume ('turn it up/down') in ONE osascript round-trip."""
    ok, out = await run_applescript(
        "set v to output volume of (get volume settings)\n"
        f"set nv to v + ({int(delta)})\n"
        "if nv > 100 then set nv to 100\n"
        "if nv < 0 then set nv to 0\n"
        "set volume output volume nv\n"
        "return nv")
    if not ok:
        return False, out
    return True, f"Volume {out.strip()}%"


async def set_muted(mute: bool) -> tuple[bool, str]:
    ok, out = await run_applescript(
        f"set volume output muted {'true' if mute else 'false'}")
    return (True, "Muted." if mute else "Unmuted.") if ok else (False, out)


# ─── String helpers ────────────────────────────────────────────────────────────

def _esc(s: str) -> str:
    """Escape for an AppleScript string literal — including newlines, which a
    raw AppleScript literal cannot contain (they're spliced via `linefeed`)."""
    s = s.replace("\\", "\\\\").replace('"', '\\"')
    return (s.replace("\r\n", "\n")
             .replace("\n", '" & linefeed & "')
             .replace("\r", '" & linefeed & "'))


def _url_encode(s: str) -> str:
    from urllib.parse import quote
    return quote(s, safe="")




