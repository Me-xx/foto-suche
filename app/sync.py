"""Syncthing-Push: ausgewählte Bilder in den lokalen Syncthing-Ordner kopieren.

Echtes Kopieren (keine Hardlinks).  Pfadstruktur konfigurierbar,
Standard: ``Bilder/Auswahl/<Jahr>/...``.  Bereits vorhandene Dateien
werden übersprungen.

Optional: nach dem Kopieren per Syncthing-REST-API einen Scan triggern
(``POST /rest/db/scan?folder=...``).
"""
from __future__ import annotations

import datetime
import os
import shutil
from pathlib import Path
from typing import Any, Optional

from .config import load_config


def _extract_year(taken: Optional[str]) -> str:
    """Extrahiert das Jahr aus dem Aufnahmedatum."""
    if taken and len(taken) >= 4:
        return taken[:4]
    return datetime.datetime.now().strftime("%Y")


def _target_path(
    src_path: str,
    sync_dir: str,
    subfolder_pattern: str,
    taken: Optional[str] = None,
) -> str:
    """Berechnet den Zielpfad im Syncthing-Ordner."""
    jahr = _extract_year(taken)
    subfolder = subfolder_pattern.replace("{jahr}", jahr)
    name = os.path.basename(src_path)
    return os.path.join(sync_dir, subfolder, name)


def push_to_syncthing(
    image_paths: list[tuple[str, Optional[str]]],  # (src_path, taken)
    sync_dir: str,
    subfolder_pattern: str = "Bilder/Auswahl/{jahr}",
) -> dict[str, Any]:
    """Kopiert Bilder in den Syncthing-Ordner.

    Parameters
    ----------
    image_paths : list of (src_path, taken)
        Quellpfad und Aufnahmedatum (für Jahres-Unterordner).
    sync_dir : str
        Lokaler Syncthing-Ordner.
    subfolder_pattern : str
        Muster mit ``{jahr}``-Platzhalter.

    Returns
    -------
    dict mit keys: copied, skipped, errors, message
    """
    copied = 0
    skipped = 0
    errors: list[str] = []

    for src_path, taken in image_paths:
        if not os.path.exists(src_path):
            errors.append(f"Nicht gefunden: {src_path}")
            continue

        dst = _target_path(src_path, sync_dir, subfolder_pattern, taken)
        os.makedirs(os.path.dirname(dst), exist_ok=True)

        if os.path.exists(dst):
            skipped += 1
            continue

        try:
            shutil.copy2(src_path, dst)
            copied += 1
        except OSError as e:
            errors.append(f"Fehler bei {src_path}: {e}")

    msg = f"{copied} kopiert, {skipped} übersprungen."
    if errors:
        msg += f" {len(errors)} Fehler."

    return {
        "copied": copied,
        "skipped": skipped,
        "errors": errors,
        "message": msg,
    }


def trigger_scan(
    api_url: str,
    api_key: str,
    folder_id: str,
) -> dict[str, Any]:
    """Triggert einen Syncthing-DB-Scan per REST-API.

    POST /rest/db/scan?folder=<folder_id>
    """
    try:
        import requests
    except ImportError:
        return {"ok": False, "message": "requests nicht installiert."}

    url = f"{api_url.rstrip('/')}/rest/db/scan"
    params = {"folder": folder_id}
    headers = {"X-API-Key": api_key}

    try:
        resp = requests.post(url, params=params, headers=headers, timeout=10)
        if resp.ok:
            return {"ok": True, "message": "Scan ausgelöst."}
        return {
            "ok": False,
            "message": f"HTTP {resp.status_code}: {resp.text[:200]}",
        }
    except Exception as e:
        return {"ok": False, "message": f"Verbindung fehlgeschlagen: {e}"}


def run_from_config(
    image_paths: list[tuple[str, Optional[str]]],
    cfg: Optional[dict] = None,
) -> dict[str, Any]:
    cfg = cfg or load_config()
    sc = cfg["syncthing"]
    result = push_to_syncthing(
        image_paths,
        sync_dir=sc["sync_dir"],
        subfolder_pattern=sc["subfolder_pattern"],
    )

    # Optional: Scan triggern
    if sc.get("api_url") and sc.get("api_key") and sc.get("folder_id"):
        scan_result = trigger_scan(
            sc["api_url"], sc["api_key"], sc["folder_id"]
        )
        result["scan"] = scan_result

    return result
