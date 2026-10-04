"""Konfiguration laden/erzeugen (config.yaml)."""
from __future__ import annotations

import copy
import os
from pathlib import Path
from typing import Any

import yaml

DEFAULT_CONFIG: dict[str, Any] = {
    "digikam": {
        # Pfad zur digiKam-Datenbank (wird strikt read-only geöffnet)
        "db_path": "C:/MeineDateienDesk/digikam/digikam4.db",
        # Bild-Root (AlbumRoot.specificPath als Windows-Pfad)
        "image_root": "C:/MeineDateienDesk/digikam",
        # Optional: Kopie der DB verwenden (falls digiKam läuft und sperrt)
        "db_copy_path": None,
    },
    "embedding": {
        "model": "BAAI/bge-m3",
        "model_cache_dir": None,
        "batch_size": 16,
        "dimension": 1024,
    },
    "index": {
        "db_path": "index.db",
        "thumbs_dir": "thumbs",
    },
    "dlna": {
        "output_dir": "C:/MeineDateienDesk/dlna/suche",
        "max_versions": 5,
    },
    "caption_check": {
        # Lokales Qwen2-VL-Verzeichnis für den LLM-Merge (INT4, GPU)
        "llm_model_path": "C:/MeineDateienDesk/MCP/models/Qwen2VL",
        # Wohin die descriptError-Excel geschrieben wird (App-Verzeichnis)
        "excel_dir": None,
    },
    "syncthing": {
        # Lokaler Syncthing-Ordner "ServerDaten" (folder id: daten, send-only)
        "sync_dir": "C:/SycnT_Server",
        "subfolder_pattern": "Bilder/Auswahl/{jahr}",
        "api_url": None,
        "api_key": None,
        "folder_id": "daten",
    },
}

# Schlüssel, deren Wert None ist → beim ersten Start als leere Config belassen,
# damit der Nutzer sie ausfüllen muss.
_REQUIRED_USER_KEYS: list[tuple[str, str]] = [
    ("syncthing", "sync_dir"),
]


def _deep_merge(base: dict, override: dict) -> dict:
    """Rekursives Merge: override gewinnt über base."""
    result = copy.deepcopy(base)
    for k, v in override.items():
        if k in result and isinstance(result[k], dict) and isinstance(v, dict):
            result[k] = _deep_merge(result[k], v)
        else:
            result[k] = v
    return result


def config_path(base_dir: str | Path | None = None) -> Path:
    """Pfad zur config.yaml (neben dem app/-Verzeichnis)."""
    if base_dir is None:
        base_dir = Path(__file__).resolve().parent.parent
    return Path(base_dir) / "config.yaml"


def load_config(base_dir: str | Path | None = None) -> dict[str, Any]:
    """Config laden.  Erzeugt config.yaml mit Defaults, falls nicht vorhanden."""
    cpath = config_path(base_dir)
    if not cpath.exists():
        cpath.parent.mkdir(parents=True, exist_ok=True)
        with open(cpath, "w", encoding="utf-8") as f:
            yaml.dump(DEFAULT_CONFIG, f, default_flow_style=False, allow_unicode=True)
        return copy.deepcopy(DEFAULT_CONFIG)

    with open(cpath, "r", encoding="utf-8") as f:
        user_cfg = yaml.safe_load(f) or {}

    return _deep_merge(DEFAULT_CONFIG, user_cfg)


def save_config(cfg: dict[str, Any], base_dir: str | Path | None = None) -> None:
    cpath = config_path(base_dir)
    cpath.parent.mkdir(parents=True, exist_ok=True)
    with open(cpath, "w", encoding="utf-8") as f:
        yaml.dump(cfg, f, default_flow_style=False, allow_unicode=True)


def resolve_path(cfg: dict[str, Any], *parts: str) -> str:
    """Hilfsfunktion: Pfad relativ zum App-Verzeichnis oder absolut auflösen."""
    p = Path(parts[0])
    if not p.is_absolute():
        p = Path(__file__).resolve().parent.parent / p
    for seg in parts[1:]:
        p = p / seg
    return str(p)
