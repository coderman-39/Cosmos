"""
Free geocoding + distance via OpenStreetMap Nominatim (no API key required).
Also handles distance calculation from user's configured home location.
"""

import os
import math
import httpx
from urllib.parse import quote

# User's home/office location — edit or override via env
# Neutral fallback (San Francisco) — set HOME_LAT/HOME_LON/HOME_LABEL in .env
HOME_LAT = float(os.getenv("HOME_LAT", "37.7749"))
HOME_LON = float(os.getenv("HOME_LON", "-122.4194"))
HOME_LABEL = os.getenv("HOME_LABEL", "Home")


async def geocode(place: str) -> tuple[float, float, str] | None:
    """
    Returns (lat, lon, display_name) for a place name.
    Uses Nominatim (OpenStreetMap) — free, no API key.
    """
    url = (
        f"https://nominatim.openstreetmap.org/search"
        f"?q={quote(place)}&format=json&limit=1&addressdetails=0"
    )
    headers = {"User-Agent": "CosmosAI/1.0 (personal assistant)"}
    async with httpx.AsyncClient(timeout=8) as c:
        try:
            r = await c.get(url, headers=headers)
            if r.status_code == 200:
                data = r.json()
                if data:
                    d = data[0]
                    return float(d["lat"]), float(d["lon"]), d.get("display_name", place)
        except Exception:
            pass
    return None


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in km."""
    R = 6371.0
    d_lat = math.radians(lat2 - lat1)
    d_lon = math.radians(lon2 - lon1)
    a = (math.sin(d_lat / 2) ** 2
         + math.cos(math.radians(lat1))
         * math.cos(math.radians(lat2))
         * math.sin(d_lon / 2) ** 2)
    return R * 2 * math.asin(math.sqrt(a))


async def distance_from_home(destination: str) -> str | None:
    """
    Calculate straight-line distance from user's home location to a destination.
    Returns a human-readable string like "8.3 km" or None on failure.
    Note: straight-line distance; road distance is typically 1.2-1.5× longer.
    """
    result = await geocode(destination)
    if not result:
        return None
    lat2, lon2, display = result
    km = haversine_km(HOME_LAT, HOME_LON, lat2, lon2)
    # Road distance is typically 20-40% longer than straight-line
    road_km = km * 1.3
    return f"approximately {road_km:.1f} km"


async def google_maps_directions_url(origin: str, destination: str) -> str:
    """Build a Google Maps directions URL."""
    return (
        f"https://www.google.com/maps/dir/"
        f"{quote(origin)}/{quote(destination)}"
    )
