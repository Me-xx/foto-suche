"""Geo-Helfer: Ortssuche (Nominatim) und Geo-Box-Berechnung in Kilometern."""
from __future__ import annotations

from typing import Any, Optional

import requests

# Erdumfang: 1 Grad Breitengrad ≈ 111.32 km (Mittelwert)
_KM_PER_DEG_LAT = 111.32

_NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
_HEADERS = {
    "User-Agent": "foto-suche-mvp/0.1 (lokale Foto-Suche)",
    "Accept-Language": "de",
}


def geocode(query: str, timeout: int = 10) -> Optional[dict[str, Any]]:
    """Sucht einen Ort über Nominatim (OpenStreetMap).

    Returns
    -------
    dict mit lat, lon, display_name — oder None bei Fehler/nicht gefunden.
    """
    try:
        resp = requests.get(
            _NOMINATIM_URL,
            params={"q": query, "format": "json", "limit": 1},
            headers=_HEADERS,
            timeout=timeout,
        )
        if not resp.ok:
            return None
        results = resp.json()
        if not results:
            return None
        r = results[0]
        return {
            "lat": float(r["lat"]),
            "lon": float(r["lon"]),
            "display_name": r.get("display_name", query),
        }
    except requests.RequestException:
        return None


def box_from_center(
    center_lat: float,
    center_lon: float,
    width_km: float,
    height_km: float,
) -> tuple[float, float, float, float]:
    """Berechnet eine Geo-Box (lat_min, lon_min, lat_max, lon_max) aus
    Mittelpunkt und Größe in Kilometern.

    width_km = Ausdehnung in Ost-West-Richtung (Längengrad),
    height_km = Ausdehnung in Nord-Süd-Richtung (Breitengrad).
    """
    d_lat = (height_km / 2.0) / _KM_PER_DEG_LAT
    cos = max(abs(center_lat), 1e-6)
    cos = min(max(cos, 0.01), 1.0)
    d_lon = (width_km / 2.0) / (_KM_PER_DEG_LAT * cos)
    return (
        center_lat - d_lat,
        center_lon - d_lon,
        center_lat + d_lat,
        center_lon + d_lon,
    )


def box_dimensions_km(
    box: tuple[float, float, float, float]
) -> tuple[float, float, float, float]:
    """Liefert (center_lat, center_lon, width_km, height_km) für eine Box."""
    lat_min, lon_min, lat_max, lon_max = box
    center_lat = (lat_min + lat_max) / 2.0
    center_lon = (lon_min + lon_max) / 2.0
    height_km = (lat_max - lat_min) * _KM_PER_DEG_LAT
    cos = min(max(abs(center_lat), 0.01), 1.0)
    width_km = (lon_max - lon_min) * _KM_PER_DEG_LAT * cos
    return center_lat, center_lon, width_km, height_km
