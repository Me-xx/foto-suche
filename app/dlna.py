"""DLNA-Auslieferung: NTFS-Hardlinks in den Ergebnis-Ordner.

Erzeugt Hardlinks der Treffer im konfigurierten DLNA-Ausgabeordner.
Hardlinks verbrauchen keinen zusätzlichen Speicherplatz.  Falls Quell-
und Zielordner auf verschiedenen Partitionen liegen, wird graceful auf
Kopieren mit Warnhinweis zurückgefallen.

Der Ergebnisordner wird versioniert: pro "Treffer an Beamer"-Aktion
entsteht ein Unterordner mit Zeitstempel.  Nur die letzten N Versionen
werden behalten.
"""
from __future__ import annotations

import datetime
import os
import shutil
from pathlib import Path
from typing import Any, Optional

from .config import load_config


def _same_partition(path_a: str, path_b: str) -> bool:
    """Prüft, ob zwei Pfade auf derselben NTFS-Partition liegen."""
    try:
        sa = os.stat(path_a)
        sb = os.stat(path_b)
        return sa.st_dev == sb.st_dev
    except OSError:
        # Wenn ein Pfad noch nicht existiert, vergleiche Laufwerksbuchstaben
        return Path(path_a).resolve().anchor.lower() == Path(path_b).resolve().anchor.lower()


def create_hardlink(src: str, dst: str) -> str:
    """Erzeugt einen NTFS-Hardlink.  Fallback auf Kopieren bei Bedarf."""
    try:
        os.link(src, dst)
        return "hardlink"
    except OSError:
        shutil.copy2(src, dst)
        return "copy"


def push_to_dlna(
    image_paths: list[str],
    output_dir: str,
    max_versions: int = 5,
) -> dict[str, Any]:
    """Erzeugt einen versionierten Unterordner mit Hardlinks/Kopien der Treffer.

    Returns
    -------
    dict mit keys:
        folder : erstellter Unterordner
        method : 'hardlink' oder 'copy'
        count  : Anzahl erzeugter Dateien
        message : Statusmeldung
    """
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)

    # Versionierter Unterordner
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    sub = out / ts
    sub.mkdir(exist_ok=True)

    if not image_paths:
        return {
            "folder": str(sub),
            "method": "none",
            "count": 0,
            "message": "Keine Treffer zum Ausliefern.",
        }

    # Partitions-Check
    sample_src = image_paths[0]
    same = _same_partition(sample_src, str(sub))
    method = "hardlink" if same else "copy"
    message = "" if same else (
        "WARNUNG: Quell- und Zielordner liegen auf verschiedenen Partitionen — "
        "es wird kopiert statt Hardlinks erzeugt (Platzverbrauch!)."
    )

    count = 0
    for i, src_path in enumerate(image_paths):
        if not os.path.exists(src_path):
            continue
        # Eindeutiger Dateiname: {index}_{originalname}
        name = os.path.basename(src_path)
        dst = sub / f"{i:04d}_{name}"
        try:
            create_hardlink(src_path, str(dst))
            count += 1
        except OSError as e:
            message += f"\nFehler bei {name}: {e}"

    # Alte Versionen aufräumen
    _cleanup_versions(out, max_versions)

    return {
        "folder": str(sub),
        "method": method,
        "count": count,
        "message": message or f"{count} Bilder als {method} in {sub} ausgeliefert.",
    }


def _cleanup_versions(base: Path, max_versions: int) -> None:
    """Behält nur die letzten N Versions-Unterordner."""
    if max_versions <= 0:
        return
    subs = sorted(
        [d for d in base.iterdir() if d.is_dir()],
        key=lambda d: d.name,
        reverse=True,
    )
    for old in subs[max_versions:]:
        shutil.rmtree(old, ignore_errors=True)


def list_versions(output_dir: str) -> list[dict[str, Any]]:
    """Listet vorhandene DLNA-Ergebnis-Ordner auf."""
    base = Path(output_dir)
    if not base.exists():
        return []
    result = []
    for d in sorted(base.iterdir(), key=lambda d: d.name, reverse=True):
        if d.is_dir():
            files = list(d.iterdir())
            result.append({
                "folder": d.name,
                "path": str(d),
                "count": len(files),
            })
    return result


def run_from_config(
    image_paths: list[str], cfg: Optional[dict] = None
) -> dict[str, Any]:
    cfg = cfg or load_config()
    return push_to_dlna(
        image_paths,
        output_dir=cfg["dlna"]["output_dir"],
        max_versions=cfg["dlna"]["max_versions"],
    )
