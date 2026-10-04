"""Read-only digiKam-Datenbank-Reader.

Öffnet die digiKam-DB strikt im SQLite-Read-only-Modus.  Schreibzugriffe
finden niemals statt — digiKam kann parallel laufen, ohne gestört zu werden.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Optional


@dataclass
class ImageRecord:
    image_id: int
    path: str
    name: str
    caption: Optional[str]
    rating: Optional[int]
    lat: Optional[float]
    lon: Optional[float]
    taken: Optional[str]


class DigiKamReader:
    """Read-only Zugriff auf die digiKam SQLite-Datenbank."""

    def __init__(self, db_path: str, image_root: str, db_copy_path: Optional[str] = None):
        self.db_path = db_path
        self.image_root = image_root.rstrip("/\\")
        self.db_copy_path = db_copy_path
        self._conn: Optional[sqlite3.Connection] = None

    @property
    def effective_db_path(self) -> str:
        return self.db_copy_path or self.db_path

    def connect(self) -> sqlite3.Connection:
        """Öffnet die DB im Read-only-Modus (immutable=1).

        immutable=1 verhindert jegliche Sperren — digiKam kann parallel
        schreiben, ohne dass wir es stören oder umgekehrt.
        """
        path = self.effective_db_path.replace("\\", "/")
        uri = f"file:{path}?mode=ro&immutable=1"
        self._conn = sqlite3.connect(uri, uri=True)
        self._conn.row_factory = sqlite3.Row
        return self._conn

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self.connect()
        return self._conn  # type: ignore

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, *exc):
        self.close()

    # -- Schema-Introspection ------------------------------------------------

    def table_columns(self, table: str) -> list[str]:
        cur = self.conn.execute(f'PRAGMA table_info("{table}")')
        return [r[1] for r in cur.fetchall()]

    # -- Datenabfrage --------------------------------------------------------

    def _build_path(self, relative_path: Optional[str], name: str) -> str:
        """Vollständigen Windows-Pfad aus Album-RelativePath + Dateiname bauen."""
        rel = (relative_path or "").replace("/", "\\").lstrip("\\")
        if rel:
            return f"{self.image_root}\\{rel}\\{name}"
        return f"{self.image_root}\\{name}"

    def iter_images(
        self,
        only_with_caption: bool = False,
        since_image_id: Optional[int] = None,
    ) -> Iterator[ImageRecord]:
        """Iteriert über alle Bilder mit Caption, Rating, Geo und Datum.

        Parameters
        ----------
        only_with_caption : bool
            Nur Bilder mit vorhandener Caption (type=1) zurückgeben.
        since_image_id : Optional[int]
            Nur Bilder mit id > since_image_id (für inkrementellen Sync).
        """
        conditions = ["i.album IS NOT NULL"]
        params: list[Any] = []

        if only_with_caption:
            conditions.append("ic.comment IS NOT NULL")
        if since_image_id is not None:
            conditions.append("i.id > ?")
            params.append(since_image_id)

        where = " AND ".join(conditions)
        sql = f"""
            SELECT i.id, a.relativePath, i.name,
                   ic.comment, ii.rating,
                   ip.latitudeNumber, ip.longitudeNumber,
                   ii.creationDate
              FROM Images i
              JOIN Albums a ON i.album = a.id
              LEFT JOIN ImageComments ic ON ic.imageid = i.id AND ic.type = 1
              LEFT JOIN ImageInformation ii ON ii.imageid = i.id
              LEFT JOIN ImagePositions ip ON ip.imageid = i.id
             WHERE {where}
             ORDER BY i.id
        """
        cur = self.conn.execute(sql, params)
        for row in cur:
            yield ImageRecord(
                image_id=row[0],
                path=self._build_path(row[1], row[2]),
                name=row[2],
                caption=row[3],
                rating=row[4],
                lat=row[5],
                lon=row[6],
                taken=row[7],
            )

    def get_tags(self) -> list[dict[str, Any]]:
        """Alle Tags mit Hierarchie (pid = Parent-ID)."""
        cur = self.conn.execute("SELECT id, pid, name FROM Tags ORDER BY name")
        return [{"tag_id": r[0], "pid": r[1], "name": r[2]} for r in cur.fetchall()]

    def get_image_tag_ids(self, image_id: int) -> list[int]:
        """Tag-IDs für ein einzelnes Bild."""
        cur = self.conn.execute(
            "SELECT tagid FROM ImageTags WHERE imageid = ?", (image_id,)
        )
        return [r[0] for r in cur.fetchall()]

    def get_image_tags_map(self, image_ids: list[int]) -> dict[int, list[int]]:
        """Mapping image_id → [tag_id, ...] für eine Menge von Bildern."""
        if not image_ids:
            return {}
        placeholders = ",".join("?" * len(image_ids))
        cur = self.conn.execute(
            f"SELECT imageid, tagid FROM ImageTags WHERE imageid IN ({placeholders})",
            image_ids,
        )
        result: dict[int, list[int]] = {}
        for r in cur.fetchall():
            result.setdefault(r[0], []).append(r[1])
        return result

    def filter_by_tags(self, tag_ids: list[int]) -> set[int]:
        """Menge der image_ids, die ALLE angegebenen Tags besitzen (AND-Semantik)."""
        if not tag_ids:
            return set()
        placeholders = ",".join("?" * len(tag_ids))
        sql = f"""
            SELECT imageid
              FROM ImageTags
             WHERE tagid IN ({placeholders})
             GROUP BY imageid
            HAVING COUNT(DISTINCT tagid) = ?
        """
        cur = self.conn.execute(sql, tag_ids + [len(tag_ids)])
        return {r[0] for r in cur.fetchall()}

    def db_mtime(self) -> float:
        """mtime der DB-Datei (für Sync-Tracking)."""
        return Path(self.effective_db_path).stat().st_mtime
