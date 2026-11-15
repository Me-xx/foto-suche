"""Diashow-Video aus ausgewählten Bildern (ffmpeg, integriert aus makeVideo.py).

Ablauf:
1. Ausgewählte Bilder werden in einen Temp-Ordner kopiert (Original bleibt
   unangetastet) und nach Aufnahmedatum sortiert (Index-taken, Fallback EXIF
   bzw. Dateidatum).
2. ffmpeg baut daraus eine Diashow mit Überblendungen (xfade) und optional
   einem MP3-Soundtrack mit Abschluss-Fade.
3. Das fertige MP4 wird in den Syncthing-Ordner (Videos/Auswahl/<Jahr>)
   kopiert.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, Optional

BILDER_ENDUNGEN = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff", ".heic"}


def find_ffmpeg() -> Optional[str]:
    """Liefert den ffmpeg-Pfad oder None (statt sys.exit — für die UI)."""
    return shutil.which("ffmpeg")


def _sort_key(path: str, taken: Optional[str]) -> tuple:
    """Sortierschlüssel: Aufnahmedatum aus dem Index, Fallback EXIF/Dateidatum."""
    if taken:
        try:
            j, m, t = int(taken[0:4]), int(taken[5:7]), int(taken[8:10])
            h, mi, s = int(taken[11:13]), int(taken[14:16]), int(taken[17:19])
            return (j, m, t, h, mi, s)
        except (ValueError, IndexError):
            pass
    lt = time.localtime(os.path.getmtime(path))
    return (lt.tm_year, lt.tm_mon, lt.tm_mday, lt.tm_hour, lt.tm_min, lt.tm_sec)


def _build_filter(
    count: int, seconds_per_image: float, fade_s: float, width: int, height: int
) -> tuple[str, float]:
    """filter_complex-Kette mit xfade-Überblendungen → (filter, gesamt_s)."""
    parts = []
    for i in range(count):
        parts.append(
            f"[{i}:v]scale={width}:{height}:force_original_aspect_ratio=decrease,"
            f"pad={width}:{height}:-1:-1:color=black,setsar=1[s{i}]"
        )

    total = seconds_per_image
    if count == 1:
        return parts[0], total

    prev = "s0"
    offset = seconds_per_image - fade_s
    for i in range(1, count):
        label = f"x{i}" if i < count - 1 else "v"
        parts.append(
            f"[{prev}][s{i}]xfade=transition=fade:duration={fade_s}:offset={offset:.3f}[{label}]"
        )
        prev = label
        offset += seconds_per_image - fade_s
        total = offset + fade_s
    return ";".join(parts), total


def create_slideshow(
    image_specs: list[tuple[str, Optional[str]]],  # (pfad, taken_iso)
    seconds_per_image: float = 4.0,
    fade_s: float = 1.0,
    resolution: str = "1920x1080",
    music_path: Optional[str] = None,
) -> dict[str, Any]:
    """Erzeugt die Diashow in einem Temp-Ordner.

    Returns: dict mit ok, video_path, temp_dir, count, duration_s, message.
    temp_dir muss vom Aufrufer nach der Weiterverwendung aufgeräumt werden
    (cleanup_temp), damit ein Fehler im Nachgang nichts zerstört.
    """
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        return {
            "ok": False,
            "message": "ffmpeg nicht gefunden — 'winget install Gyan.FFmpeg' "
            "ausführen oder PATH prüfen.",
        }

    # Nur existierende Bilddateien
    specs = [(p, t) for p, t in image_specs if os.path.isfile(p)]
    if not specs:
        return {"ok": False, "message": "Keine vorhandenen Bilder ausgewählt."}

    specs.sort(key=lambda s: (_sort_key(s[0], s[1]), os.path.basename(s[0]).lower()))

    # Temp-Ordner: Kopie der Bilder (Original bleibt unangetastet)
    temp_dir = tempfile.mkdtemp(prefix="foto_suche_slideshow_")
    work: list[str] = []
    for i, (path, _taken) in enumerate(specs):
        dst = os.path.join(temp_dir, f"{i:05d}_{os.path.basename(path)}")
        shutil.copy2(path, dst)
        work.append(dst)

    width, height = (int(x) for x in resolution.lower().split("x"))
    out_path = os.path.join(
        temp_dir, f"Diashow_{time.strftime('%Y%m%d_%H%M%S')}.mp4"
    )

    cmd = [ffmpeg, "-y"]
    for b in work:
        cmd += ["-loop", "1", "-t", str(seconds_per_image), "-i", b]
    if music_path and os.path.isfile(music_path):
        cmd += ["-i", music_path]

    filt, total = _build_filter(len(work), seconds_per_image, fade_s, width, height)
    if music_path and os.path.isfile(music_path):
        m = len(work)
        filt += (
            f";[{m}:a]aformat=sample_rates=48000:channel_layouts=stereo,"
            f"afade=t=out:st={max(0, total - 3):.2f}:d=3,apad[a]"
        )

    cmd += ["-filter_complex", filt, "-map", "[v]"]
    if music_path and os.path.isfile(music_path):
        cmd += ["-map", "[a]"]
    cmd += ["-c:v", "libx264", "-pix_fmt", "yuv420p", "-r", "30"]
    if music_path and os.path.isfile(music_path):
        cmd += ["-c:a", "aac", "-b:a", "192k", "-t", f"{total:.3f}"]
    cmd += [out_path]

    r = subprocess.run(
        cmd, capture_output=True, text=True, encoding="utf-8", errors="replace"
    )
    if r.returncode != 0 or not os.path.isfile(out_path):
        tail = (r.stderr or "")[-800:]
        return {
            "ok": False,
            "message": f"ffmpeg-Fehler (Code {r.returncode}): …{tail}",
            "temp_dir": temp_dir,
        }

    size_mb = os.path.getsize(out_path) / (1024 * 1024)
    return {
        "ok": True,
        "video_path": out_path,
        "temp_dir": temp_dir,
        "count": len(work),
        "duration_s": total,
        "message": (
            f"Diashow mit {len(work)} Bildern, ~{total:.0f} s "
            f"({size_mb:.1f} MB) erzeugt."
        ),
    }


def cleanup_temp(temp_dir: Optional[str]) -> None:
    """Räumt den Temp-Ordner auf (inkl. Kopien und fertiger MP4 dort)."""
    if temp_dir:
        shutil.rmtree(temp_dir, ignore_errors=True)


def push_video_to_syncthing(
    video_path: str,
    sync_dir: str,
    subfolder_pattern: str = "Videos/Auswahl/{jahr}",
    taken_default: Optional[str] = None,
) -> dict[str, Any]:
    """Kopiert das fertige Video in den Syncthing-Ordner (Videos statt Bilder)."""
    jahr = None
    if taken_default:
        jahr = taken_default[:4]
    if not jahr or not jahr.isdigit():
        jahr = time.strftime("%Y")
    subfolder = subfolder_pattern.replace("{jahr}", jahr)
    dst = os.path.join(sync_dir, subfolder, os.path.basename(video_path))
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    if os.path.exists(dst):
        return {"ok": True, "dest": dst, "copied": False,
                "message": "Video existiert bereits (übersprungen)."}
    shutil.copy2(video_path, dst)
    return {
        "ok": True,
        "dest": dst,
        "copied": True,
        "message": f"Video nach {dst} kopiert — Syncthing synchronisiert "
        "automatisch Richtung Homelab.",
    }
