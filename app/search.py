"""Hybrid-Suche: SQL-Vorfilterung + Vektor-Ranking (NumPy Cosine).

SQL-Filter (Tags, Bewertung, Datum, Geo-Box) werden zuerst auf die
Index-DB angewendet.  Bei einer Freitext-Suche wird die Query embedded
und per exakter Cosine-Ähnlichkeit (NumPy-Matrixmultiplikation) gegen
die Teilmenge der bereits embedded Bilder gerankt.

Embeddings liegen als float32-BLOB in der ``images``-Tabelle und werden
beim App-Start (bzw. beim ersten Suchaufruf) einmalig in ein NumPy-Array
geladen (~600 MB bei 150k × 1024 × 4 B).  Ein Suchdurchlauf dauert
unter 100 ms.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import numpy as np

from .config import load_config


@dataclass
class SearchHit:
    image_id: int
    path: str
    name: str
    caption: Optional[str]
    rating: Optional[int]
    lat: Optional[float]
    lon: Optional[float]
    taken: Optional[str]
    score: Optional[float]   # Cosine-Ähnlichkeit (nur bei semantischer Suche)
    tags: list[str]


def open_index_db(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


class SearchEngine:
    def __init__(self, cfg: Optional[dict] = None, base_dir: str | Path | None = None):
        self.cfg = cfg or load_config(base_dir)
        index_db = self.cfg["index"]["db_path"]
        if not Path(index_db).is_absolute():
            index_db = str(Path(__file__).resolve().parent.parent / index_db)
        self.index_db = index_db
        self.dimension = self.cfg["embedding"]["dimension"]
        self._conn: Optional[sqlite3.Connection] = None
        self._model = None

        # NumPy-Vektor-Cache (lazy geladen)
        self._emb_ids: Optional[np.ndarray] = None     # (N,) image_ids
        self._emb_matrix: Optional[np.ndarray] = None  # (N, dim) float32, L2-normalized

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = open_index_db(self.index_db)
        return self._conn

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None
        self._emb_ids = None
        self._emb_matrix = None

    # -- Vektor-Cache -------------------------------------------------------

    def _load_embeddings(self) -> None:
        """Lädt alle embedded Embeddings aus der DB in ein NumPy-Array.

        Einmalig beim ersten Suchaufruf.  Danach liegen die Vektoren als
        (N, dim) float32-Matrix mit L2-normalisierten Zeilen im RAM.
        """
        if self._emb_matrix is not None:
            return

        cur = self.conn.execute(
            "SELECT image_id, embedding FROM images WHERE embedded = 1 AND embedding IS NOT NULL"
        )
        ids: list[int] = []
        blobs: list[bytes] = []
        for row in cur:
            ids.append(row[0])
            blobs.append(row[1])

        if not ids:
            self._emb_ids = np.array([], dtype=np.int64)
            self._emb_matrix = np.zeros((0, self.dimension), dtype=np.float32)
            return

        # BLOBs in eine zusammenhängende Matrix dekodieren
        raw = b"".join(blobs)
        matrix = np.frombuffer(raw, dtype=np.float32).reshape(len(ids), -1)

        # Dimensionalität anpassen, falls das Modell eine andere liefert
        if matrix.shape[1] != self.dimension:
            self.dimension = int(matrix.shape[1])

        # L2-Norm pro Zeile — falls schon normalisiert (sentence-transformers
        # mit normalize_embeddings=True), bleibt die Norm 1.0.  Defensive
        # Renormalisierung schadet nicht.
        norms = np.linalg.norm(matrix, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        matrix = matrix / norms

        self._emb_ids = np.array(ids, dtype=np.int64)
        self._emb_matrix = matrix.astype(np.float32)

    def reload_embeddings(self) -> None:
        """Erzwingt ein Neuladen des Vektor-Caches (z.B. nach Embedding-Lauf)."""
        self._emb_ids = None
        self._emb_matrix = None
        self._load_embeddings()

    # -- Tag-Zugriff --------------------------------------------------------

    def get_tags(self) -> list[dict[str, Any]]:
        cur = self.conn.execute(
            "SELECT tag_id, name, pid FROM tags ORDER BY name"
        )
        return [{"tag_id": r[0], "name": r[1], "pid": r[2]} for r in cur.fetchall()]

    def get_tag_name(self, tag_id: int) -> str:
        cur = self.conn.execute("SELECT name FROM tags WHERE tag_id = ?", (tag_id,))
        row = cur.fetchone()
        return row[0] if row else str(tag_id)

    # -- Embedding-Modell ---------------------------------------------------

    def _load_model(self):
        if self._model is None:
            import torch
            from sentence_transformers import SentenceTransformer
            device = "cuda" if torch.cuda.is_available() else "cpu"
            model_kwargs = {}
            if self.cfg["embedding"].get("model_cache_dir"):
                model_kwargs["cache_folder"] = self.cfg["embedding"]["model_cache_dir"]
            self._model = SentenceTransformer(
                self.cfg["embedding"]["model"], device=device, **model_kwargs
            )
        return self._model

    # -- Suche --------------------------------------------------------------

    def search(
        self,
        query: str = "",
        tag_ids: list[int] | None = None,
        min_rating: int = 0,
        date_from: str | None = None,
        date_to: str | None = None,
        geo_box: tuple[float, float, float, float] | None = None,
        limit: int = 100,
    ) -> list[SearchHit]:
        """Hybride Suche.

        1. SQL-Vorfilterung (Tags, Bewertung, Datum, Geo-Box)
        2. Falls query: Vektor-Ranking (NumPy Cosine) auf der Teilmenge
        3. Falls keine query: Sortierung nach Datum (neueste zuerst)
        """
        conditions: list[str] = []
        params: list[Any] = []

        # Tag-Filter (AND-Semantik)
        if tag_ids:
            placeholders = ",".join("?" * len(tag_ids))
            conditions.append(f"""
                image_id IN (
                    SELECT image_id FROM image_tags
                     WHERE tag_id IN ({placeholders})
                     GROUP BY image_id
                    HAVING COUNT(DISTINCT tag_id) = ?
                )
            """)
            params.extend(tag_ids)
            params.append(len(tag_ids))

        # Bewertungs-Filter (rating >= min_rating; -1 = unbewertet → als 0 behandeln)
        if min_rating > 0:
            conditions.append("(rating IS NOT NULL AND rating >= ?)")
            params.append(min_rating)

        # Datumsbereich
        if date_from:
            conditions.append("(taken IS NOT NULL AND taken >= ?)")
            params.append(date_from)
        if date_to:
            conditions.append("(taken IS NOT NULL AND taken <= ?)")
            params.append(date_to)

        # Geo-Box (lat_min, lon_min, lat_max, lon_max)
        if geo_box:
            lat_min, lon_min, lat_max, lon_max = geo_box
            conditions.append(
                "(lat IS NOT NULL AND lon IS NOT NULL AND "
                "lat >= ? AND lat <= ? AND lon >= ? AND lon <= ?)"
            )
            params.extend([lat_min, lat_max, lon_min, lon_max])

        where = " AND ".join(conditions) if conditions else "1=1"

        # --- Fall 1: Semantische Suche ---
        if query.strip():
            return self._vector_search(query, where, params, limit)

        # --- Fall 2: Nur SQL-Filter ---
        sql = f"""
            SELECT image_id, path, name, caption, rating, lat, lon, taken
              FROM images
             WHERE {where}
             ORDER BY taken DESC NULLS LAST
             LIMIT ?
        """
        params.append(limit)
        cur = self.conn.execute(sql, params)
        rows = cur.fetchall()
        return self._rows_to_hits(rows, score=None)

    def _vector_search(
        self, query: str, where: str, params: list[Any], limit: int
    ) -> list[SearchHit]:
        # Query embedden und L2-normalisieren
        model = self._load_model()
        q_emb = model.encode([query], normalize_embeddings=True)[0]
        q_vec = np.asarray(q_emb, dtype=np.float32).reshape(1, -1)
        # Defensive Renormalisierung
        q_norm = np.linalg.norm(q_vec, axis=1, keepdims=True)
        q_norm[q_norm == 0] = 1.0
        q_vec = q_vec / q_norm

        # Embedding-Cache laden (einmalig)
        self._load_embeddings()

        if self._emb_matrix is None or len(self._emb_ids) == 0:
            # Keine Embeddings vorhanden → Volltext-Fallback
            return self._fallback_text_search(query, where, params, limit)

        # Cosine-Ähnlichkeit = Skalarprodukt (beide L2-normalisiert)
        # scores: (N,) = q_vec (1, dim) @ matrix.T (dim, N)
        scores = (q_vec @ self._emb_matrix.T).ravel()  # (N,)

        # Top-K Kandidaten (über alle embedded Bilder, bevor SQL-Filter greifen)
        k = max(limit * 5, 200)
        k = min(k, len(scores))
        top_idx = np.argpartition(-scores, k - 1)[:k]
        # Innerhalb der Top-K nach Score absteigend sortieren
        top_idx = top_idx[np.argsort(-scores[top_idx])]

        candidate_ids = self._emb_ids[top_idx]
        candidate_scores = {int(cid): float(scores[i]) for i, cid in zip(top_idx, candidate_ids)}

        # SQL-Vorfilterung auf Kandidaten anwenden
        placeholders = ",".join("?" * len(candidate_ids))
        sql = f"""
            SELECT image_id, path, name, caption, rating, lat, lon, taken
              FROM images
             WHERE image_id IN ({placeholders})
               AND ({where})
        """
        cur = self.conn.execute(sql, list(int(c) for c in candidate_ids) + params)
        rows = cur.fetchall()

        # Nach Cosine-Score absteigend sortieren (höher = besser)
        hits = self._rows_to_hits(
            rows,
            score_map=lambda iid: candidate_scores.get(iid),
            sort_key=lambda h: h.score if h.score is not None else -1.0,
            reverse_sort=True,
        )
        return hits[:limit]

    def _fallback_text_search(
        self, query: str, where: str, params: list[Any], limit: int
    ) -> list[SearchHit]:
        """Volltext-Fallback: LIKE-Suche in Caption (für nicht-embedded Bilder)."""
        like = f"%{query}%"
        sql = f"""
            SELECT image_id, path, name, caption, rating, lat, lon, taken
              FROM images
             WHERE ({where})
               AND (caption LIKE ? COLLATE NOCASE)
             ORDER BY taken DESC NULLS LAST
             LIMIT ?
        """
        cur = self.conn.execute(sql, params + [like, limit])
        rows = cur.fetchall()
        return self._rows_to_hits(rows, score=None)

    def _rows_to_hits(
        self,
        rows: list,
        score: Optional[float] = None,
        score_map=None,
        sort_key=None,
        reverse_sort: bool = True,
    ) -> list[SearchHit]:
        # Tag-Namen für alle Treffer sammeln
        ids = [r[0] for r in rows]
        tag_names: dict[int, list[str]] = {}
        if ids:
            placeholders = ",".join("?" * len(ids))
            cur = self.conn.execute(
                f"""
                SELECT it.image_id, t.name
                  FROM image_tags it JOIN tags t ON it.tag_id = t.tag_id
                 WHERE it.image_id IN ({placeholders})
                """,
                ids,
            )
            for r in cur.fetchall():
                tag_names.setdefault(r[0], []).append(r[1])

        hits: list[SearchHit] = []
        for r in rows:
            s = score_map(r[0]) if score_map else score
            hits.append(SearchHit(
                image_id=r[0],
                path=r[1],
                name=r[2],
                caption=r[3],
                rating=r[4],
                lat=r[5],
                lon=r[6],
                taken=r[7],
                score=s,
                tags=tag_names.get(r[0], []),
            ))

        if sort_key:
            hits.sort(key=sort_key, reverse=reverse_sort)
        return hits

    def stats(self) -> dict[str, Any]:
        from .index import stats as _stats
        return _stats(self.conn)
