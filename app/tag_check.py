"""Tag-Cleanup: gleichnamige digiKam-Tags (ignore case) zusammenführen.

Aufruf ohne Parameter (Analyse):
    python -m app.tag_check
        → gruppiert alle Tags nach Name (case-insensitive; ein führendes
          "Person:"-Präfix wird ignoriert, d.h. "Person:Anika" gilt als
          Dublette zu "Anika") und schreibt eine Excel
          ``tagMerge_<Datum-Uhrzeit>.xlsx`` mit den Spalten
          A) ID, B) Tag Name, C) Tag Parent, D) neue Tag ID.
          Bei Namensgleichheit wird der Tag mit den meisten Bild-Zuordnungen
          beibehalten (Vorschlag in Spalte D schon eingetragen).

Aufruf mit Excel-Parameter (Anwenden):
    python -m app.tag_check <excel.xlsx> [--dry-run] [--db <pfad>]
        → führt die Zusammenführungen durch:
          * ImageTags-Zuordnungen des alten Tags werden auf den Ziel-Tag
            übertragen (Duplikate entfallen),
          * der alte Tag inkl. TagProperties/ImageTagProperties wird gelöscht.
        Danach werden alle leeren Tags gelöscht — leer heißt: keine Bilder
        UND keine Kind-Tags. Parents mit Kind-Tags bleiben also stehen;
        werden sie durch das Löschen ihrer letzten Kinder selbst leer und
        kinderlos, werden sie im nächsten Durchlauf ebenfalls gelöscht.
        digiKam-interne Tags (Farbmarken etc. unter "_Digikam_Internal_Tags_")
        bleiben unberührt.
        Zeilen ohne Eintrag in 'neue Tag ID' werden übersprungen; nur was in
        der Excel steht und bestätigt ist, wird verändert.  Vor dem ersten
        Write wird eine Sicherungskopie der digikam4.db angelegt.
"""
from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any, Optional

from .config import load_config

# ---------------------------------------------------------------------------
# Analyse → Excel
# ---------------------------------------------------------------------------


# Präfixe, die bei der Dubletten-Suche ignoriert werden: "Person:Anika" ist
# dasselbe wie "Anika" (mit Parent Person).
_IGNORED_PREFIXES = ("person",)


def _norm(name: str) -> str:
    """Normalisiert einen Tag-Namen: trimmen, casefold, Präfix entfernen."""
    n = (name or "").strip().casefold()
    for prefix in _IGNORED_PREFIXES:
        if n.startswith(prefix + ":"):
            n = n[len(prefix) + 1:].strip()
            break
    return n


def analyze(cfg: Optional[dict] = None) -> list[dict[str, Any]]:
    """Findet Tag-Gruppen mit gleichem Namen (case-insensitive, ohne
    "Person:"-Präfix)."""
    cfg = cfg or load_config()
    db = cfg["digikam"]["db_path"]
    conn = sqlite3.connect(f"file:{db}?mode=ro&immutable=1", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        tags = conn.execute("SELECT id, pid, name FROM Tags").fetchall()
        counts = dict(
            conn.execute("SELECT tagid, COUNT(*) FROM ImageTags GROUP BY tagid")
        )
        names = {t["id"]: t["name"] for t in tags}
    finally:
        conn.close()

    groups: dict[str, list[sqlite3.Row]] = {}
    for t in tags:
        groups.setdefault(_norm(t["name"]), []).append(t)

    rows: list[dict[str, Any]] = []
    for key in sorted(groups):
        members = groups[key]
        if len(members) < 2:
            continue
        # Gewinner: die meisten Zuordnungen, bei Gleichstand kleinste ID
        members.sort(key=lambda t: (-counts.get(t["id"], 0), t["id"]))
        winner = members[0]
        for t in members:
            rows.append(
                {
                    "id": t["id"],
                    "name": t["name"],
                    "parent": names.get(t["pid"], "") or "",
                    "count": counts.get(t["id"], 0),
                    # Gewinner behält sich selbst (leere Spalte D), Rest → Gewinner
                    "new_id": "" if t["id"] == winner["id"] else winner["id"],
                    "winner_name": "" if t["id"] == winner["id"] else winner["name"],
                }
            )
    return rows


def write_excel(rows: list[dict[str, Any]], out_path: Path) -> None:
    from openpyxl import Workbook
    from openpyxl.styles import Font

    wb = Workbook()
    ws = wb.active
    ws.title = "Tag-Merge"
    headers = ["ID", "Tag Name", "Tag Parent", "neue Tag ID", "Ziel-Name", "Bilder"]
    ws.append(headers)
    for cell in ws[1]:
        cell.font = Font(bold=True)

    for r in rows:
        ws.append([r["id"], r["name"], r["parent"], r["new_id"],
                   r["winner_name"], r["count"]])

    for i, w in enumerate([8, 42, 42, 14, 42, 10], start=1):
        ws.column_dimensions[ws.cell(row=1, column=i).column_letter].width = w
    wb.save(out_path)


def run_analyze(cfg: dict) -> Path:
    rows = analyze(cfg)
    n_groups = len({_norm(r["name"]) for r in rows})
    out_dir = Path(__file__).resolve().parent.parent
    out = out_dir / f"tagMerge_{time.strftime('%Y%m%d_%H%M%S')}.xlsx"
    write_excel(rows, out)
    print(f"{len(rows)} Tags in {n_groups} Namens-Gruppen mit Dubletten gefunden.")
    print(f"Excel geschrieben: {out}")
    print(
        "Vorschlag: Spalte 'neue Tag ID' zeigt das Merge-Ziel (Tag mit den "
        "meisten Zuordnungen bleibt).\nHinweis: führendes 'Person:' wird "
        "ignoriert (Person:Anika gilt als Dublette zu Anika).\n"
        "Zum Anwenden ggf. Ziele anpassen/leeren und dann\n"
        "  python -m app.tag_check <excel>  aufrufen."
    )
    return out


# ---------------------------------------------------------------------------
# Excel → digiKam-DB
# ---------------------------------------------------------------------------


def read_merges(excel_path: Path) -> tuple[dict[int, int], list[str]]:
    """Liest (Quell-ID → Ziel-ID) aus der Excel; leere 'neue Tag ID' = skip."""
    import openpyxl

    wb = openpyxl.load_workbook(excel_path, data_only=True)
    ws = wb.active
    header = {}
    for col, cell in enumerate(ws[1], start=1):
        if cell.value is not None:
            header[str(cell.value).strip()] = col
    for required in ("ID", "neue Tag ID"):
        if required not in header:
            raise SystemExit(
                f"Spalte '{required}' nicht gefunden — ist das eine "
                f"tagMerge-Datei?"
            )

    merges: dict[int, int] = {}
    warnings: list[str] = []
    for row in ws.iter_rows(min_row=2, values_only=True):
        src = row[header["ID"] - 1]
        dst = row[header["neue Tag ID"] - 1]
        if src is None or dst is None or str(dst).strip() == "":
            continue
        try:
            src, dst = int(src), int(dst)
        except (TypeError, ValueError):
            warnings.append(f"Ungültige Zeile übersprungen: ID={src!r}, Ziel={dst!r}")
            continue
        if src == dst:
            continue
        if src in merges and merges[src] != dst:
            warnings.append(f"Widersprüchliche Ziele für Tag {src} — letzter gewinnt.")
        merges[src] = dst
    return merges, warnings


def _resolve_chains(merges: dict[int, int]) -> dict[int, int]:
    """Falls A→B und B→C: löst zu A→C auf (Ziele, die selbst gemerged werden)."""
    resolved = {}
    for src in merges:
        dst = merges[src]
        seen = {src}
        while dst in merges and dst not in seen:
            seen.add(dst)
            dst = merges[dst]
        resolved[src] = dst
    return resolved


def _delete_tag(conn: sqlite3.Connection, tag_id: int) -> bool:
    """Löscht einen Tag samt Properties — nur wenn er keine Kind-Tags hat.

    Tags mit Kindern bleiben stehen, sonst reißt die Tag-Hierarchie ab.
    """
    children = conn.execute(
        "SELECT COUNT(*) FROM Tags WHERE pid = ?", (tag_id,)
    ).fetchone()[0]
    if children:
        return False
    conn.execute("DELETE FROM TagProperties WHERE tagid = ?", (tag_id,))
    conn.execute("DELETE FROM ImageTagProperties WHERE tagid = ?", (tag_id,))
    conn.execute("DELETE FROM Tags WHERE id = ?", (tag_id,))
    return True


_INTERNAL_ROOT = "_digikam_internal_tags_"


def _internal_tag_ids(conn: sqlite3.Connection) -> set[int]:
    """IDs aller digiKam-internen Tags (Unterbaum '_Digikam_Internal_Tags_').

    Diese darf man nie löschen — digiKam nutzt sie intern (Farbmarken,
    Pick-Labels etc.), unabhängig davon, ob Bilder zugeordnet sind.
    """
    rows = conn.execute("SELECT id, pid, name FROM Tags").fetchall()
    children: dict[Any, list[int]] = {}
    roots: list[int] = []
    for r in rows:
        children.setdefault(r["pid"], []).append(r["id"])
        if (r["name"] or "").strip().casefold() == _INTERNAL_ROOT:
            roots.append(r["id"])
    internal = set(roots)
    stack = list(roots)
    while stack:
        for child in children.get(stack.pop(), []):
            if child not in internal:
                internal.add(child)
                stack.append(child)
    return internal


def _empty_tags(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Leere Tags: keine Bilder UND keine Kind-Tags.

    digiKam-interne Tags sind ausgenommen. Eltern mit Kind-Tags bleiben
    grundsätzlich stehen.
    """
    rows = conn.execute("SELECT id, name FROM Tags").fetchall()
    parents = {
        r[0] for r in conn.execute("SELECT DISTINCT pid FROM Tags WHERE pid IS NOT NULL")
    }
    counts = dict(
        conn.execute("SELECT tagid, COUNT(*) FROM ImageTags GROUP BY tagid")
    )
    internal = _internal_tag_ids(conn)
    return [
        r
        for r in rows
        if r["id"] not in parents
        and counts.get(r["id"], 0) == 0
        and r["id"] not in internal
    ]


def run_apply(
    excel_path: Path,
    cfg: dict,
    dry_run: bool = False,
    db_override: Optional[str] = None,
) -> int:
    db_path = db_override or cfg["digikam"]["db_path"]
    merges, warnings = read_merges(excel_path)
    for w in warnings:
        print(f"  WARNUNG: {w}")
    if merges:
        merges = _resolve_chains(merges)

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    existing = {r[0] for r in conn.execute("SELECT id FROM Tags")}
    empty_before = _empty_tags(conn)
    conn.close()

    plan: dict[int, int] = {}
    for src, dst in merges.items():
        if src not in existing:
            print(f"  Übersprungen: Quell-Tag {src} existiert nicht (bereits gemerged?)")
            continue
        if dst not in existing:
            print(f"  Übersprungen: Ziel-Tag {dst} existiert nicht (Zeile mit ID {src})")
            continue
        plan[src] = dst

    if not plan and not empty_before:
        print("Keine Zusammenführungen markiert und keine leeren Tags — nichts zu tun.")
        return 0

    if plan:
        print(f"{len(plan)} Tag-Merges:")
        for src, dst in sorted(plan.items()):
            print(f"  {src} → {dst}")
    else:
        print("Keine Zusammenführungen markiert.")
    print(
        f"\n{len(empty_before)} leere Tags (ohne Bilder und ohne Kind-Tags) "
        "werden zusätzlich gelöscht:"
    )
    for r in sorted(empty_before, key=lambda x: (x["name"] or "").casefold()):
        print(f"  {r['id']}: {r['name']}")
    print(
        "Hinweis: durch die Merges können weitere Tags leer werden (z.B. "
        "Parents, deren letzte Kinder gelöscht wurden) — auch diese werden "
        "gelöscht."
    )
    if dry_run:
        print("--- DRY-RUN: es wird nichts geschrieben. ---")
        return 0

    bak = Path(db_path).with_suffix(f".{time.strftime('%Y%m%d_%H%M%S')}.bak")
    shutil.copy2(db_path, bak)
    print(f"\nDB-Backup angelegt: {bak}")

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("BEGIN IMMEDIATE")
    except sqlite3.OperationalError as e:
        conn.close()
        raise SystemExit(
            f"digiKam-Datenbank ist gesperrt ({e}) — digiKam schließen und "
            "erneut versuchen."
        )

    n_moved = n_dupes = 0
    deleted: list[int] = []
    kept_with_children: list[int] = []
    empty_deleted: list[tuple[int, str]] = []
    try:
        for src, dst in sorted(plan.items()):
            cur = conn.execute(
                "INSERT OR IGNORE INTO ImageTags(imageid, tagid) "
                "SELECT imageid, ? FROM ImageTags WHERE tagid = ?",
                (dst, src),
            )
            n_moved += max(cur.rowcount or 0, 0)
            cur = conn.execute(
                "DELETE FROM ImageTags WHERE tagid = ?", (src,)
            )
            n_dupes += max(cur.rowcount or 0, 0)
            rest = conn.execute(
                "SELECT COUNT(*) FROM ImageTags WHERE tagid = ?", (src,)
            ).fetchone()[0]
            if rest == 0:
                if _delete_tag(conn, src):
                    deleted.append(src)
                else:
                    kept_with_children.append(src)
            else:
                print(f"  WARNUNG: Tag {src} behält {rest} Zuordnungen (unverändert).")

        # Nach dem Neu-Zuordnen: alle leeren Tags löschen (keine Bilder,
        # keine Kind-Tags). Werden Parents durch das Löschen ihrer letzten
        # Kinder selbst leer und kinderlos, wandern sie im nächsten
        # Durchlauf ebenfalls in die Löschung. digiKam-interne Tags
        # (Farbmarken etc.) bleiben unberührt.
        while True:
            empties = _empty_tags(conn)
            if not empties:
                break
            for r in empties:
                _delete_tag(conn, r["id"])
                empty_deleted.append((r["id"], r["name"]))
        conn.commit()
    except Exception:
        conn.rollback()
        conn.close()
        raise
    finally:
        try:
            conn.close()
        except Exception:
            pass

    n_dupes = max(n_dupes - n_moved, 0)
    print(
        f"Fertig: {len(deleted)} Quell-Tags gelöscht, "
        f"{n_moved} Zuordnungen zum Ziel übertragen, "
        f"davon {n_dupes} Dubletten entfernt."
    )
    if empty_deleted:
        print(f"{len(empty_deleted)} leere Tags gelöscht:")
        for tid, name in sorted(empty_deleted, key=lambda x: (x[1] or "").casefold()):
            print(f"  {tid}: {name}")
    if kept_with_children:
        print(
            f"INFO: {len(kept_with_children)} Tags behalten, weil sie "
            f"Kind-Tags haben (Hierarchie): {kept_with_children}"
        )
    print(
        "Hinweis: danach 'python -m app.index --sync-only' laufen lassen, "
        "damit der Tag-Index aktualisiert wird."
    )
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.tag_check",
        description=(
            "Gleichnamige digiKam-Tags (ignore case) finden, Merge-Excel "
            "erzeugen und markierte Merges anwenden."
        ),
    )
    parser.add_argument(
        "excel", nargs="?", default=None,
        help="tagMerge-Excel; ohne diesen Parameter läuft nur die Analyse.",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Nur anzeigen, was passieren würde (keine DB-Änderung).",
    )
    parser.add_argument(
        "--db", default=None,
        help="Abweichenden digiKam-DB-Pfad (z.B. Testkopie) verwenden.",
    )
    args = parser.parse_args()

    cfg = load_config()
    if args.excel is None:
        run_analyze(cfg)
    else:
        p = Path(args.excel)
        if not p.exists():
            print(f"Excel nicht gefunden: {p}", file=sys.stderr)
            return 1
        run_apply(p, cfg, dry_run=args.dry_run, db_override=args.db)
    return 0


if __name__ == "__main__":
    sys.exit(main())
