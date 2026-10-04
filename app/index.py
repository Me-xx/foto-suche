"""sqlite-Index + Vektor-Embeddings (CLI: ``python -m app.index``).

Speichert Bild-Metadaten und Embeddings der Qwen-Captions in einer
eigenen SQLite-Datei (``index.db``).  Embeddings werden als BLOB
(float32-Bytes) direkt in der ``images``-Tabelle gespeichert — keine
sqlite-vec-Erweiterung nötig.  Die Cosine-Suche läuft zur Laufzeit über
NumPy (exakt, RAM-basiert — bei ~150k × 1024 × 4 B ≈ 600 MB).

Der Embedding-Lauf ist inkrementell und idempotent: nur neue/geänderte
Captions werden neu embeddet.  Abbrechbar/resumierbar — nach jedem Batch
wird committet.
"""
from __future__ import annotations

import argparse
import hashlib
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any, Optional

from .config import load_config
from .digikam import DigiKamReader

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS images (
    image_id     INTEGER PRIMARY KEY,
    path         TEXT,
    name         TEXT,
    caption      TEXT,
    caption_hash TEXT,
    rating       INTEGER,
    lat          REAL,
    lon          REAL,
    taken        TEXT,
    embedding    BLOB,
    embedded     INTEGER DEFAULT 0,
    embedded_at  TEXT,
    updated_at   TEXT
);

CREATE TABLE IF NOT EXISTS tags (
    tag_id INTEGER PRIMARY KEY,
    name   TEXT,
    pid    INTEGER
);

CREATE TABLE IF NOT EXISTS image_tags (
    image_id INTEGER,
    tag_id   INTEGER,
    PRIMARY KEY (image_id, tag_id)
);

CREATE INDEX IF NOT EXISTS idx_image_tags_tid ON image_tags(tag_id);
CREATE INDEX IF NOT EXISTS idx_images_rating ON images(rating);
CREATE INDEX IF NOT EXISTS idx_images_embedded ON images(embedded);
"""

# Migration: ALTER TABLE falls die Spalte 'embedding' noch fehlt
_MIGRATION_EMBEDDING = "ALTER TABLE images ADD COLUMN embedding BLOB"


def caption_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def open_index_db(db_path: str, dimension: int = 1024) -> sqlite3.Connection:
    """Öffnet die Index-DB (ohne Extensions — reines sqlite3)."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA_SQL)

    # Migration: embedding-Spalte nachträglich hinzufügen, falls die
    # Tabelle aus einer älteren Version ohne diese Spalte stammt.
    cols = [r[1] for r in conn.execute("PRAGMA table_info(images)")]
    if "embedding" not in cols:
        conn.execute(_MIGRATION_EMBEDDING)

    conn.commit()
    return conn


def sync_metadata(
    dk: DigiKamReader, conn: sqlite3.Connection, batch_size: int = 500
) -> dict[str, int]:
    """Synchronisiert Bild-Metadaten und Tags aus digiKam in die Index-DB.

    Inkrementell: nur Bilder mit id > last_sync_id werden aktualisiert.
    """
    cur = conn.execute(
        "SELECT value FROM meta WHERE key = 'last_sync_image_id'"
    )
    row = cur.fetchone()
    since = int(row[0]) if row else 0

    n_images = 0
    batch: list[tuple] = []

    for rec in dk.iter_images(since_image_id=since):
        ch = caption_hash(rec.caption) if rec.caption else None
        batch.append(
            (
                rec.image_id,
                rec.path,
                rec.name,
                rec.caption,
                ch,
                rec.rating,
                rec.lat,
                rec.lon,
                rec.taken,
            )
        )
        if len(batch) >= batch_size:
            _upsert_images(conn, batch)
            n_images += len(batch)
            batch = []
            conn.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES ('last_sync_image_id', ?)",
                (str(rec.image_id),),
            )
            conn.commit()
            print(f"\r  Metadaten: {n_images} Bilder synchronisiert", end="", flush=True)

    if batch:
        _upsert_images(conn, batch)
        n_images += len(batch)

    if n_images > 0:
        conn.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES ('last_sync_image_id', ?)",
            (str(rec.image_id),),
        )

    # Tags synchronisieren (Vollsync — Tags sind klein)
    conn.execute("DELETE FROM tags")
    conn.execute("DELETE FROM image_tags")
    tags = dk.get_tags()
    conn.executemany(
        "INSERT INTO tags(tag_id, name, pid) VALUES (?, ?, ?)",
        [(t["tag_id"], t["name"], t["pid"]) for t in tags],
    )
    n_tags = len(tags)

    # ImageTags für alle Bilder (batchweise)
    cur = conn.execute("SELECT image_id FROM images")
    all_ids = [r[0] for r in cur.fetchall()]
    tag_map = dk.get_image_tags_map(all_ids)
    tag_batch: list[tuple] = []
    for img_id, tids in tag_map.items():
        for tid in tids:
            tag_batch.append((img_id, tid))
            if len(tag_batch) >= batch_size:
                conn.executemany(
                    "INSERT OR IGNORE INTO image_tags(image_id, tag_id) VALUES (?, ?)",
                    tag_batch,
                )
                tag_batch = []
    if tag_batch:
        conn.executemany(
            "INSERT OR IGNORE INTO image_tags(image_id, tag_id) VALUES (?, ?)",
            tag_batch,
        )

    conn.execute(
        "INSERT OR REPLACE INTO meta(key, value) VALUES ('last_sync_at', ?)",
        (time.strftime("%Y-%m-%dT%H:%M:%S"),),
    )
    conn.commit()

    print(f"\r  Metadaten: {n_images} Bilder, {n_tags} Tags synchronisiert.        ")
    return {"images": n_images, "tags": n_tags}


def _upsert_images(conn: sqlite3.Connection, batch: list[tuple]) -> None:
    """Upsert ohne embedding-Spalte (wird nur beim Embedding-Lauf gesetzt)."""
    conn.executemany(
        """
        INSERT INTO images (image_id, path, name, caption, caption_hash,
                             rating, lat, lon, taken)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(image_id) DO UPDATE SET
            path=excluded.path, name=excluded.name,
            caption=excluded.caption, caption_hash=excluded.caption_hash,
            rating=excluded.rating, lat=excluded.lat, lon=excluded.lon,
            taken=excluded.taken,
            updated_at=CURRENT_TIMESTAMP
        """,
        batch,
    )


def _embedding_to_blob(emb: Any) -> bytes:
    """Konvertiert ein NumPy-Array (oder list) in float32-BLOB."""
    import numpy as np

    arr = np.asarray(emb, dtype=np.float32)
    return arr.tobytes()


def _blob_to_embedding(blob: bytes, dimension: int) -> Any:
    """Deserialisiert einen float32-BLOB zurück in ein NumPy-Array."""
    import numpy as np

    return np.frombuffer(blob, dtype=np.float32).copy()


def run_embeddings(
    conn: sqlite3.Connection,
    model_name: str,
    batch_size: int = 16,
    dimension: int = 1024,
    model_cache_dir: Optional[str] = None,
    limit: Optional[int] = None,
) -> dict[str, int]:
    """Embeddet alle Captions, die noch nicht embedded sind (inkrementell).

    Abbrechbar: nach jedem Batch wird committet.  Beim erneuten Aufruf
    wird dort weitergemacht, wo abgebrochen wurde.
    """
    print(f"Lade Embedding-Modell '{model_name}' …")
    import torch
    from sentence_transformers import SentenceTransformer

    device = "cuda" if torch.cuda.is_available() else "cpu"
    print(f"  Device: {device}")

    model_kwargs: dict[str, Any] = {}
    if model_cache_dir:
        model_kwargs["cache_folder"] = model_cache_dir

    model = SentenceTransformer(model_name, device=device, **model_kwargs)
    actual_dim = model.get_sentence_embedding_dimension()
    if actual_dim != dimension:
        print(f"  WARNUNG: Modell-Dimension {actual_dim} != config {dimension}, verwende {actual_dim}")
        dimension = actual_dim

    # Bilder, die neu embeddet werden müssen (embedded = 0)
    sql = """
        SELECT image_id, caption
          FROM images
         WHERE caption IS NOT NULL
           AND caption != ''
           AND (embedded = 0 OR embedded IS NULL)
         ORDER BY image_id
    """
    if limit:
        sql += f" LIMIT {limit}"

    cur = conn.execute(sql)
    rows = cur.fetchall()

    if not rows:
        print("Keine neuen Captions zu embedden — alles aktuell.")
        return {"embedded": 0, "skipped": 0}

    print(f"  {len(rows)} Captions zu embedden …")

    n_done = 0
    buf_ids: list[int] = []
    buf_texts: list[str] = []

    for row in rows:
        buf_ids.append(row[0])
        buf_texts.append(row[1])
        if len(buf_texts) >= batch_size:
            _embed_batch(conn, model, buf_ids, buf_texts)
            n_done += len(buf_ids)
            buf_ids = []
            buf_texts = []
            print(f"\r  Embedding: {n_done} / {len(rows)}", end="", flush=True)

    if buf_ids:
        _embed_batch(conn, model, buf_ids, buf_texts)
        n_done += len(buf_ids)

    conn.execute(
        "INSERT OR REPLACE INTO meta(key, value) VALUES ('last_embed_at', ?)",
        (time.strftime("%Y-%m-%dT%H:%M:%S"),),
    )
    conn.commit()
    print(f"\r  Embedding fertig: {n_done} Bilder embeddet.            ")
    return {"embedded": n_done, "skipped": 0}


def _embed_batch(
    conn: sqlite3.Connection,
    model: Any,
    ids: list[int],
    texts: list[str],
) -> None:
    embeddings = model.encode(texts, normalize_embeddings=True, show_progress_bar=False)

    for img_id, emb in zip(ids, embeddings):
        blob = _embedding_to_blob(emb)
        conn.execute(
            "UPDATE images SET embedded = 1, embedding = ?, embedded_at = CURRENT_TIMESTAMP WHERE image_id = ?",
            (blob, img_id),
        )
    conn.commit()


def stats(conn: sqlite3.Connection) -> dict[str, Any]:
    """Schnelle Statistiken über den Index."""
    cur = conn.execute("SELECT COUNT(*) FROM images")
    total = cur.fetchone()[0]
    cur = conn.execute("SELECT COUNT(*) FROM images WHERE caption IS NOT NULL AND caption != ''")
    with_caption = cur.fetchone()[0]
    cur = conn.execute("SELECT COUNT(*) FROM images WHERE embedded = 1")
    embedded = cur.fetchone()[0]
    cur = conn.execute("SELECT COUNT(*) FROM tags")
    tags = cur.fetchone()[0]
    cur = conn.execute("SELECT COUNT(*) FROM image_tags")
    image_tags = cur.fetchone()[0]
    return {
        "total_images": total,
        "with_caption": with_caption,
        "embedded": embedded,
        "tags": tags,
        "image_tags": image_tags,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.index",
        description="digiKam-Metadaten synchronisieren und Vektor-Embeddings erzeugen.",
    )
    parser.add_argument(
        "--sync-only", action="store_true",
        help="Nur Metadaten/Tags synchronisieren, keine Embeddings.",
    )
    parser.add_argument(
        "--embed-only", action="store_true",
        help="Nur Embeddings berechnen (Metadaten müssen bereits synchronisiert sein).",
    )
    parser.add_argument(
        "--limit", type=int, default=None,
        help="Maximale Anzahl Captions für Embedding (für Tests).",
    )
    parser.add_argument(
        "--stats", action="store_true",
        help="Nur Index-Statistiken ausgeben.",
    )
    parser.add_argument(
        "--reset", action="store_true",
        help="Index zurücksetzen (full rebuild).",
    )
    args = parser.parse_args()

    cfg = load_config()
    index_db = cfg["index"]["db_path"]
    if not Path(index_db).is_absolute():
        index_db = str(Path(__file__).resolve().parent.parent / index_db)

    if args.reset:
        import os
        if os.path.exists(index_db):
            os.remove(index_db)
            print(f"Index gelöscht: {index_db}")

    dim = cfg["embedding"]["dimension"]
    conn = open_index_db(index_db, dim)

    if args.stats:
        s = stats(conn)
        print("Index-Statistiken:")
        for k, v in s.items():
            print(f"  {k}: {v}")
        conn.close()
        return 0

    results: dict[str, Any] = {}

    if not args.embed_only:
        print("=== Metadaten-Sync ===")
        dk = DigiKamReader(
            db_path=cfg["digikam"]["db_path"],
            image_root=cfg["digikam"]["image_root"],
            db_copy_path=cfg["digikam"].get("db_copy_path"),
        )
        try:
            results.update(sync_metadata(dk, conn))
        finally:
            dk.close()

    if not args.sync_only:
        print("=== Embedding ===")
        results.update(
            run_embeddings(
                conn,
                model_name=cfg["embedding"]["model"],
                batch_size=cfg["embedding"]["batch_size"],
                dimension=cfg["embedding"]["dimension"],
                model_cache_dir=cfg["embedding"].get("model_cache_dir"),
                limit=args.limit,
            )
        )

    print("\nZusammenfassung:", results)
    s = stats(conn)
    print("Index jetzt:")
    for k, v in s.items():
        print(f"  {k}: {v}")
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
