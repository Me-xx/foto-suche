#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""
Diashow aus allen Bildern eines Ordners + optional ein MP3 als Musik.

Die Bilder werden in eine concat-Liste geschrieben und ffmpeg als EIN
Input uebergeben (-f concat) -- die Kommandozeile bleibt dadurch kurz,
egal wie viele Bilder es sind. Dauer pro Bild = duration in der Liste.

Aufruf:
    python makeVideo.py                          # Bilder aus dem aktuellen Ordner, ohne Musik
    python makeVideo.py -d C:\Fotos\2026        # Bildordner per Parameter
    python makeVideo.py -d C:\Fotos\2026 lied.mp3              # Musik relativ zum Bildordner
    python makeVideo.py -d C:\Fotos\2026 lied.mp3 -t 5         # 5 Sekunden pro Bild
    python makeVideo.py -d C:\Fotos\2026 lied.mp3 --fit-musik  # Dauer pro Bild so waehlen, dass das Lied voll ausgeschoepft wird
    python makeVideo.py -d C:\Fotos\2026 lied.mp3 -o out.mp4

Voraussetzung: ffmpeg muss im PATH sein (Windows: winget install Gyan.FFmpeg
oder ffmpeg.org-Download, "ffmpeg -version" testen).
"""

import argparse
import os
import shutil
import subprocess
import sys
import tempfile

BILDER_ENDUNGEN = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff", ".heic"}

# Pillow benutzen, falls vorhanden (empfohlen): "pip install pillow"
try:
    from PIL import Image, ImageOps
except ImportError:
    Image = None
    ImageOps = None


def _datum_aus_text(text: str) -> tuple | None:
    """'2021:09:15 12:00:00' -> Tupel (J,M,T,h,m,s)."""
    try:
        j, m, t = text[0:4], text[5:7], text[8:10]
        st, mi, se = text[11:13], text[14:16], text[17:19]
        return (int(j), int(m), int(t), int(st), int(mi), int(se))
    except (ValueError, IndexError):
        return None


def exif_aufnahmedatum(pfad: str) -> tuple:
    """Liest das Aufnahmedatum aus den EXIF-Daten (JPEG/TIFF).

    Bevorzugt per Pillow (falls installiert), sonst per eingebautem Parser.
    """
    if Image is not None:
        try:
            img = Image.open(pfad)
            exif = img.getexif()
            sub = exif.get_ifd(0x8769)          # Exif-Sub-IFD
            for feld in (36867, 306):           # DateTimeOriginal, DateTime
                roh = sub.get(feld) if feld == 36867 else exif.get(feld)
                if roh:
                    d = _datum_aus_text(str(roh))
                    if d:
                        return d, "EXIF"
        except Exception:
            pass
        return None, None
    return _exif_manuell(pfad)


def _exif_manuell(pfad: str) -> tuple:
    """Eingebauter EXIF-Parser ohne externe Abhaengigkeiten (Rueckfallebene)."""
    try:
        with open(pfad, "rb") as f:
            anfang = f.read(2)
            if anfang == b"\xff\xd8":            # JPEG
                f.seek(0)
                dat = f.read()
                pos = 2
                while pos < len(dat) - 1:
                    if dat[pos] != 0xFF:
                        pos += 1
                        continue
                    marker = dat[pos + 1]
                    if marker in (0xD8, 0x00) or 0xD0 <= marker <= 0xD7:
                        pos += 2
                        continue
                    if marker == 0xDA:           # Start of Scan: keine EXIF mehr
                        break
                    laenge = int.from_bytes(dat[pos + 2:pos + 4], "big")
                    if marker == 0xE1 and dat[pos + 4:pos + 10] == b"Exif\x00\x00":
                        return _exif_datum_parsen(dat, pos + 10), "EXIF"
                    pos += 2 + laenge
            elif anfang in (b"II*\x00", b"MM\x00*"):   # TIFF
                with open(pfad, "rb") as f2:
                    dat = f2.read()
                return _exif_datum_parsen(dat, 0), "EXIF"
    except OSError:
        pass
    return None, None


def _lies_datumsfeld(dat: bytes, tiff_start: int, e: int,
                     u32) -> tuple | None:
    """Liest ein Datumsfeld (20 ASCII-Zeichen) an Eintrag-Position e."""
    anzahl = u32(dat[e + 4:e + 8])
    wert_off = u32(dat[e + 8:e + 12])
    if anzahl >= 20 and wert_off:
        roh = dat[tiff_start + wert_off:tiff_start + wert_off + 19]
        try:
            text = roh.decode("ascii")
            j, m, t = text[0:4], text[5:7], text[8:10]
            st, mi, se = text[11:13], text[14:16], text[17:19]
            return (int(j), int(m), int(t), int(st), int(mi), int(se))
        except (ValueError, UnicodeDecodeError):
            pass
    return None


def _ifd_durchsuchen(dat: bytes, tiff_start: int, ifd_off: int,
                     u16, u32, sub_ifd: bool) -> tuple | None:
    """Durchsucht ein IFD nach DateTimeOriginal bzw. DateTime.

    sub_ifd=True: direkt nach 36867/306 suchen (Exif-Sub-IFD).
    sub_ifd=False: im IFD0 zuerst dem Zeiger auf das Exif-Sub-IFD
    (Tag 34965) folgen und dort suchen, sonst DateTime (306) nehmen.
    """
    p = tiff_start + ifd_off
    n = u16(dat[p:p + 2])
    exif_zeiger = None
    datum = None
    for i in range(n):
        e = p + 2 + i * 12
        tag = u16(dat[e:e + 2])
        if tag == 36867:
            datum = _lies_datumsfeld(dat, tiff_start, e, u32) or datum
        elif tag == 306:
            datum = datum or _lies_datumsfeld(dat, tiff_start, e, u32)
        elif tag == 34965 and not sub_ifd:
            # Zeiger auf das Exif-Sub-IFD (LONG, Wert steht direkt im Eintrag)
            exif_zeiger = u32(dat[e + 8:e + 12])
    if exif_zeiger and not sub_ifd:
        sub = _ifd_durchsuchen(dat, tiff_start, exif_zeiger, u16, u32, True)
        # DateTimeOriginal aus dem Sub-IFD hat Vorrang vor IFD0-DateTime
        datum = sub or datum
    return datum


def _exif_datum_parsen(dat: bytes, tiff_start: int) -> tuple | None:
    """Findet DateTimeOriginal (36867) im Exif-Sub-IFD, sonst DateTime (306)."""
    bo = dat[tiff_start:tiff_start + 2]
    little = bo == b"II"
    def u16(b): return int.from_bytes(b, "little" if little else "big")
    def u32(b): return int.from_bytes(b, "little" if little else "big")
    ifd_off = u32(dat[tiff_start + 4:tiff_start + 8])
    return _ifd_durchsuchen(dat, tiff_start, ifd_off, u16, u32, False)


def finde_bilder(ordner: str) -> list[str]:
    eintraege = []
    for name in os.listdir(ordner):
        pfad = os.path.join(ordner, name)
        if os.path.isfile(pfad) and os.path.splitext(name)[1].lower() in BILDER_ENDUNGEN:
            datum, quelle = exif_aufnahmedatum(pfad)
            if datum is None:
                # Fallback: Aenderungsdatum als sortierfaehiges Tupel
                import time
                lt = time.localtime(os.path.getmtime(pfad))
                datum = (lt.tm_year, lt.tm_mon, lt.tm_mday,
                         lt.tm_hour, lt.tm_min, lt.tm_sec)
                quelle = "Dateidatum"
            eintraege.append((datum, quelle, name, pfad))
    # Nach Aufnahmedatum, bei Gleichstand alphabetisch
    eintraege.sort(key=lambda e: (e[0], e[2]))
    for datum, quelle, name, pfad in eintraege:
        print(f"  {datum[0]:04d}-{datum[1]:02d}-{datum[2]:02d} "
              f"{datum[3]:02d}:{datum[4]:02d}:{datum[5]:02d}  [{quelle}]  {name}")
    return [e[3] for e in eintraege]


def bereite_bilder(bilder: list[str], zielordner: str) -> tuple[list[str], int]:
    """Kopiert die Bilder nach zielordner und rechnet die EXIF-Orientierung
    physisch ein (ffmpeg ignoriert das Orientation-Tag -> Bilder waeren im
    Video verdreht). Bilder ohne Drehung werden 1:1 kopiert.

    Rueckgabe: (liste der vorbereiteten Pfade, Anzahl gedrehter Bilder)
    """
    out = []
    gedreht = 0
    for i, b in enumerate(bilder):
        ziel = os.path.join(zielordner, f"{i:05d}_{os.path.basename(b)}")
        ori = 1
        if Image is not None:
            try:
                ori = int(Image.open(b).getexif().get(274, 1)) or 1
            except Exception:
                ori = 1
        if ori in (0, 1) or ImageOps is None:
            shutil.copy2(b, ziel)
        else:
            try:
                img = ImageOps.exif_transpose(Image.open(b))
                ext = os.path.splitext(ziel)[1].lower()
                if ext in (".jpg", ".jpeg", ".png", ".webp", ".bmp"):
                    img.save(ziel)
                else:
                    ziel = os.path.splitext(ziel)[0] + ".jpg"
                    img.convert("RGB").save(ziel, quality=95)
                gedreht += 1
            except Exception:
                shutil.copy2(b, ziel)
        out.append(ziel)
    return out, gedreht


def finde_ffmpeg() -> str:
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    print("FEHLER: ffmpeg nicht im PATH gefunden.")
    print("Windows: winget install Gyan.FFmpeg  (oder von ffmpeg.org laden).")
    sys.exit(1)


def musik_dauer_s(pfad: str) -> float:
    """Ermittelt die Laenge einer Audiodatei in Sekunden per ffprobe."""
    exe = shutil.which("ffprobe")
    if not exe:
        kandidate = os.path.join(os.path.dirname(finde_ffmpeg()), "ffprobe.exe")
        exe = kandidate if os.path.isfile(kandidate) else "ffprobe"
    pr = subprocess.run([exe, "-v", "error", "-show_entries", "format=duration",
                         "-of", "default=nw=1:nk=1", pfad],
                        capture_output=True, text=True)
    if pr.returncode != 0 or not pr.stdout.strip():
        print(f"FEHLER: ffprobe konnte die Dauer von {pfad} nicht ermitteln.")
        sys.exit(1)
    return float(pr.stdout.strip())


def baue_vf(breite: int, hoehe: int) -> str:
    """Video-Filterkette: skalieren, auf Zielgroesse padden, yuv420p."""
    return (f"scale={breite}:{hoehe}:force_original_aspect_ratio=decrease,"
            f"pad={breite}:{hoehe}:-1:-1:color=black,setsar=1,format=yuv420p")


def main() -> None:
    ap = argparse.ArgumentParser(description="Diashow aus einem Bildordner rendern")
    ap.add_argument("musik", nargs="?", help="MP3/WAV-Datei als Musikuntermalung (relativ zum Bildordner oder absolut)")
    ap.add_argument("-d", "--ordner", default=".", help="Ordner mit den Bildern (Default: aktueller Ordner)")
    ap.add_argument("-t", "--dauer", type=float, default=4.0, help="Sekunden pro Bild (Default 4)")
    ap.add_argument("-r", "--aufloesung", default="1920x1080", help="Zielaufloesung (Default 1920x1080)")
    ap.add_argument("-o", "--output", default="show.mp4", help="Ausgabedatei (Default show.mp4, relativ zum Bildordner)")
    ap.add_argument("--fit-musik", action="store_true",
                    help="Dauer pro Bild automatisch so setzen, dass die Musik voll ausgeschoepft wird")
    args = ap.parse_args()

    ordner = os.path.abspath(args.ordner)
    if not os.path.isdir(ordner):
        print(f"FEHLER: Ordner nicht gefunden: {ordner}")
        sys.exit(1)
    output = args.output if os.path.isabs(args.output) else os.path.join(ordner, args.output)

    bilder = finde_bilder(ordner)
    if not bilder:
        print(f"FEHLER: keine Bilder in {ordner} gefunden.")
        sys.exit(1)
    print(f"{len(bilder)} Bilder gefunden.")

    # EXIF-Orientierung physisch einrechnen, bevor die concat-Liste gebaut wird
    prep_dir = tempfile.mkdtemp(prefix="makeVideo_bilder_")
    bilder, gedreht = bereite_bilder(bilder, prep_dir)
    if gedreht:
        print(f"{gedreht} Bilder per EXIF-Orientierung gedreht.")

    # Musik: Pfad relativ zum Bildordner, sonst relativ zum aktuellen Ordner/ absolut
    musik = None
    if args.musik:
        p = args.musik if os.path.isabs(args.musik) else os.path.join(ordner, args.musik)
        if not os.path.isfile(p):
            p = os.path.abspath(args.musik)
        if not os.path.isfile(p):
            print(f"FEHLER: Musikdatei nicht gefunden: {args.musik} "
                  f"(weder in {ordner} noch im aktuellen Ordner)")
            sys.exit(1)
        musik = p

    if args.fit_musik:
        if not musik:
            print("FEHLER: --fit-musik erfordert eine Musikdatei.")
            sys.exit(1)
        md = musik_dauer_s(musik)
        n = len(bilder)
        # Gesamtlaenge = n * dauer  =>  dauer = musiklaenge / n
        args.dauer = md / n
        print(f"Musiklaenge: {md:.3f} s -> Dauer pro Bild: {args.dauer:.3f} s ({n} Bilder)")

    breite, hoehe = (int(x) for x in args.aufloesung.lower().split("x"))
    ffmpeg = finde_ffmpeg()

    # Bilder in eine concat-Datei schreiben: ffmpeg bekommt EINEN Input
    # (-f concat), die Kommandozeile bleibt kurz -- egal wie viele Bilder
    # (kein WinError 206 "Dateiname zu lang" mehr).
    fd, concat_datei = tempfile.mkstemp(prefix="makeVideo_concat_", suffix=".txt")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write("ffconcat version 1.0\n")
        for b in bilder:
            p = b.replace("\\", "/").replace("'", "\\'")
            f.write(f"file '{p}'\n")
            f.write(f"duration {args.dauer:.6f}\n")
        # letzte Datei wiederholen, sonst ignoriert der concat-Demuxer
        # die letzte duration
        p = bilder[-1].replace("\\", "/").replace("'", "\\'")
        f.write(f"file '{p}'\n")

    n = len(bilder)
    gesamt = n * args.dauer   # VFR: jedes Bild dauert args.dauer Sekunden

    cmd = [ffmpeg, "-y", "-f", "concat", "-safe", "0", "-i", concat_datei]
    if musik:
        cmd += ["-i", musik, "-map", "0:v", "-map", "1:a"]
    else:
        cmd += ["-map", "0:v"]
    cmd += ["-vf", baue_vf(breite, hoehe)]
    if musik:
        # Audio per -af auf exakt gesamt Sekunden schneiden (atrim) und
        # am Ende ausblenden (afade). Video laeuft VFR bis N*dauer + 0.04 s
        # (der wiederholte letzte Frame) -- ohne -r/fps, das die duration
        # des concat-Streams verzerren wuerde.
        cmd += ["-af", (f"aformat=sample_rates=48000:channel_layouts=stereo,"
                        f"atrim=0:{gesamt:.3f},asetpts=PTS-STARTPTS,"
                        f"afade=t=out:st={max(0, gesamt - 3):.2f}:d=3")]
    cmd += ["-c:v", "libx264", "-pix_fmt", "yuv420p"]
    if musik:
        cmd += ["-c:a", "aac", "-b:a", "192k"]
    cmd += [output]

    print(f"Gesamtlaenge des Videos: ~{gesamt:.0f} Sekunden.")
    print("Starte ffmpeg ...")
    try:
        r = subprocess.run(cmd)
    except OSError as e:
        print(f"FEHLER: ffmpeg konnte nicht gestartet werden: {e}")
        r = None
    finally:
        os.remove(concat_datei)
        shutil.rmtree(prep_dir, ignore_errors=True)
    if r is None:
        sys.exit(1)
    if r.returncode != 0:
        print("FEHLER: ffmpeg wurde mit Fehlercode", r.returncode, "beendet.")
        sys.exit(r.returncode)
    if musik:
        pr = subprocess.run(["ffprobe",
                             "-v", "error", "-select_streams", "a",
                             "-show_entries", "stream=codec_name,duration",
                             "-of", "default=nw=1", output],
                            capture_output=True, text=True)
        if pr.returncode == 0 and pr.stdout.strip():
            print("Audio-Spur im Video gefunden:")
            print(pr.stdout.strip())
        else:
            print("WARNUNG: keine Audio-Spur im Video! Pruefe die Musikdatei "
                  "(ffprobe -i lied.mp3) und melde die ffmpeg-Ausgabe.")
    print(f"Fertig: {os.path.abspath(output)}")


if __name__ == "__main__":
    main()