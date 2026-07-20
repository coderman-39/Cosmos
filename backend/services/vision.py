"""
Screen analysis via Claude Vision.
Takes a screenshot and asks Claude what's on it, enabling Cosmos to
navigate apps, read content, and do complex UI-based tasks.
"""

import os
import base64
import asyncio
from pathlib import Path

from services import llm
from services.llm import extract_text

WORK_DIR = Path.home() / "Desktop" / "cosmos-workspace"
WORK_DIR.mkdir(parents=True, exist_ok=True)


SCREENSHOT_FAILED = (
    "Error: screenshot failed — grant Screen Recording permission to Terminal "
    "in System Settings → Privacy & Security."
)


# Vision uploads: a raw Retina PNG is 3-8 MB — downscaling to ≤1440px JPEG q80
# cuts that ~10-20x with no loss the model cares about, saving 1-3s of upload
# per see_screen call.
VISION_MAX_WIDTH = int(os.getenv("FRIDAY_VISION_MAX_WIDTH", "1440"))


async def _downscale_for_vision(src: str) -> tuple[str, str]:
    """Convert `src` PNG to a ≤VISION_MAX_WIDTH JPEG via sips. Returns
    (path, media_type); falls back to the original PNG on any failure."""
    jpg = src.rsplit(".", 1)[0] + ".jpg"
    try:
        args = ["sips", "-s", "format", "jpeg", "-s", "formatOptions", "80"]
        # Only resample when actually wider — sips would UPSCALE otherwise.
        probe = await asyncio.create_subprocess_exec(
            "sips", "-g", "pixelWidth", src,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        out, _ = await asyncio.wait_for(probe.communicate(), timeout=10)
        try:
            width = int(out.decode().strip().rsplit(":", 1)[-1])
        except Exception:
            width = 0
        if width > VISION_MAX_WIDTH:
            args += ["--resampleWidth", str(VISION_MAX_WIDTH)]
        proc = await asyncio.create_subprocess_exec(
            *args, src, "--out", jpg,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        await asyncio.wait_for(proc.communicate(), timeout=15)
        if proc.returncode == 0 and os.path.exists(jpg) and os.path.getsize(jpg) > 1000:
            return jpg, "image/jpeg"
    except Exception:
        pass
    return src, "image/png"


async def _take_screenshot(path: str | None = None) -> tuple[str, str] | None:
    """Take a screenshot and return (path, media_type), or None if capture
    failed. The result is downscaled/JPEG-compressed for the vision upload.

    The stale file at the fixed path is removed FIRST so a failed capture can
    never silently analyze a screenshot from a previous run.
    """
    dest = path or str(WORK_DIR / "screen_analysis.png")
    try:
        os.remove(dest)
    except OSError:
        pass
    proc = await asyncio.create_subprocess_exec(
        "screencapture", "-x", dest,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    await proc.communicate()
    if proc.returncode != 0 or not os.path.exists(dest):
        return None
    return await _downscale_for_vision(dest)


def _encode_image(path: str) -> str:
    with open(path, "rb") as f:
        return base64.standard_b64encode(f.read()).decode()


async def analyze_screen(
    question: str,
    model: str,
    screenshot_path: str | None = None,
    on_fallback=None,
) -> str:
    """
    Take a screenshot and ask the vision model what's on the screen.
    Returns a natural language answer to the question. Uses the fast
    fallback chain — every model in it is vision-capable (benchmarked).
    """
    shot = await _take_screenshot(screenshot_path)
    if shot is None:
        return SCREENSHOT_FAILED
    path, media_type = shot
    img_b64 = _encode_image(path)

    response = await llm.acreate(
        model=model,
        fallbacks=llm.FAST_FALLBACKS,
        on_fallback=on_fallback,
        max_tokens=1500,
        messages=[{
            "role": "user",
            "content": [
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": media_type,
                        "data": img_b64,
                    },
                },
                {
                    "type": "text",
                    "text": (
                        f"{question}\n\n"
                        "Be specific and factual about what you see. "
                        "Include exact text, names, numbers you can read. "
                        "If asked about messages, quotes them directly."
                    ),
                },
            ],
        }],
    )
    return extract_text(response)


async def find_element_on_screen(
    element_description: str,
    model: str,
) -> dict | None:
    """
    Find a UI element on screen and return its approximate location.
    Returns {"found": bool, "description": str, "location": str, "action": str}
    """
    shot = await _take_screenshot()
    if shot is None:
        return None
    path, media_type = shot
    img_b64 = _encode_image(path)

    prompt = (
        f"Look at this screenshot. Find: '{element_description}'\n\n"
        "If you find it, respond with ONLY this JSON (no other text):\n"
        # Plain (non-f) string fragments — single braces render literally.
        '{"found": true, "element": "exact text/label you see", '
        '"location": "top-left|top-center|top-right|center-left|center|center-right|bottom-left|bottom-center|bottom-right", '
        '"action": "what to do — click/type/scroll"}\n\n'
        "If not found: {\"found\": false, \"reason\": \"why\"}"
    )

    import json as _json, re as _re
    response = await llm.acreate(
        model=model, fallbacks=llm.FAST_FALLBACKS, max_tokens=200,
        messages=[{
            "role": "user",
            "content": [
                {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": img_b64}},
                {"type": "text", "text": prompt},
            ],
        }],
    )
    raw = extract_text(response).strip()
    try:
        m = _re.search(r'\{.*\}', raw, _re.DOTALL)
        return _json.loads(m.group()) if m else None
    except Exception:
        return None


async def read_screen_content(
    target: str,
    model: str,
) -> str:
    """
    Read specific content from the screen.
    target: e.g. "the last 5 messages in this conversation",
                 "the job titles visible on this page",
                 "my profile name and title"
    """
    return await analyze_screen(
        f"Extract and list the following from what you see on screen: {target}. "
        "Quote the exact text you can read. Be precise and complete.",
        model,
    )
