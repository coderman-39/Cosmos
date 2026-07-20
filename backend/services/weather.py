"""Weather via Open-Meteo (free, no API key required).

Responses are cached for FRIDAY_WEATHER_TTL seconds (default 600) — weather
doesn't change minute-to-minute, and the fast path in main.py plus the boot
push both hit this, so one network call serves many lookups.
"""

import os
import json
import time
import asyncio

WMO_DESCRIPTIONS = {
    0: "Clear sky", 1: "Mainly clear", 2: "Partly cloudy", 3: "Overcast",
    45: "Foggy", 48: "Icy fog", 51: "Light drizzle", 53: "Moderate drizzle",
    55: "Dense drizzle", 61: "Light rain", 63: "Moderate rain", 65: "Heavy rain",
    80: "Rain showers", 81: "Moderate showers", 82: "Violent showers",
    95: "Thunderstorm", 96: "Thunderstorm with hail",
}

# Mumbai coordinates (default)
DEFAULT_LAT  = 19.076
DEFAULT_LON  = 72.877
DEFAULT_CITY = "Mumbai"

_TTL = float(os.getenv("FRIDAY_WEATHER_TTL", "600"))
# (lat, lon) → (expires_monotonic, payload)
_cache: dict[tuple, tuple[float, dict]] = {}


def _hour_index(data: dict) -> int:
    """Index of the CURRENT hour in the hourly arrays — index 0 is midnight,
    which made feels-like/humidity up to 23 hours stale."""
    try:
        now_prefix = data["current_weather"]["time"][:13]      # "2026-07-08T14"
        times = data["hourly"]["time"]
        for i, t in enumerate(times):
            if t.startswith(now_prefix):
                return i
    except Exception:
        pass
    return 0


async def get_current(lat=DEFAULT_LAT, lon=DEFAULT_LON, city=DEFAULT_CITY) -> dict:
    key = (round(lat, 3), round(lon, 3))
    hit = _cache.get(key)
    if hit and hit[0] > time.monotonic():
        return hit[1]

    url = (
        f"https://api.open-meteo.com/v1/forecast"
        f"?latitude={lat}&longitude={lon}"
        f"&current_weather=true"
        f"&hourly=relativehumidity_2m,apparent_temperature"
        f"&timezone=auto"
    )
    # Use system `curl` (macOS keychain trust) — Python 3.13's httpx hits
    # "CERTIFICATE_VERIFY_FAILED" on this machine (same reason web_search uses curl).
    proc = await asyncio.create_subprocess_exec(
        "curl", "-s", "--max-time", "8", url,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
    except (asyncio.TimeoutError, TimeoutError):
        try: proc.kill()
        except ProcessLookupError: pass
        raise

    data = json.loads(out.decode())
    cw = data["current_weather"]
    h = _hour_index(data)
    result = {
        "temp": round(cw["temperature"]),
        "feelsLike": round(data["hourly"]["apparent_temperature"][h]),
        "humidity": data["hourly"]["relativehumidity_2m"][h],
        "windspeed": round(cw.get("windspeed", 0)),   # km/h
        "description": WMO_DESCRIPTIONS.get(cw["weathercode"], "Variable"),
        "location": city,
        "code": cw["weathercode"],
    }
    _cache[key] = (time.monotonic() + _TTL, result)
    return result
