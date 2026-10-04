"""Streamlit-Frontend für die Foto-Suche.

Start: ``streamlit run app/ui.py``

Features:
- Freitext-Suche (semantisch, mit Fallback auf Volltext)
- Filter: Tags (Multiselect), Bewertungs-Minimum, Datumsbereich, Geo-Box
- Thumbnail-Grid mit Checkboxen pro Treffer
- Button „Treffer an Beamer" → Hardlinks im DLNA-Ordner
- Button „Auswahl → Homelab" → Kopieren in Syncthing-Ordner
"""
from __future__ import annotations

import io
import os
import sys
from pathlib import Path

import streamlit as st

# Sicherstellen, dass das Paket als 'app' importierbar ist
_pkg_root = str(Path(__file__).resolve().parent.parent)
if _pkg_root not in sys.path:
    sys.path.insert(0, _pkg_root)

from app.config import load_config, save_config
from app.search import SearchEngine
from app.dlna import run_from_config as dlna_push
from app.sync import run_from_config as sync_push


# ---------------------------------------------------------------------------
# Thumbnail-Erzeugung (mit Cache)
# ---------------------------------------------------------------------------

@st.cache_data(show_spinner=False)
def _thumb_bytes(path: str, size: int = 200, thumbs_root: str = "thumbs") -> bytes | None:
    """Erzeugt ein skaliertes JPEG-Thumbnail und cacht es auf der Festplatte."""
    from PIL import Image

    cache_dir = Path(thumbs_root)
    cache_dir.mkdir(parents=True, exist_ok=True)
    # Cache-Dateiname: Hash des Pfads
    import hashlib
    key = hashlib.md5(path.encode("utf-8")).hexdigest()
    cache_file = cache_dir / f"{key}.jpg"

    if cache_file.exists():
        return cache_file.read_bytes()

    if not os.path.exists(path):
        return None

    try:
        with Image.open(path) as img:
            img = img.convert("RGB")
            img.thumbnail((size, size))
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=85)
            data = buf.getvalue()
            cache_file.write_bytes(data)
            return data
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Session-State
# ---------------------------------------------------------------------------

def init_state():
    if "selected" not in st.session_state:
        st.session_state.selected = set()  # image_ids
    if "last_results" not in st.session_state:
        st.session_state.last_results = []


def get_engine() -> SearchEngine:
    if "_engine" not in st.session_state:
        st.session_state._engine = SearchEngine()
    return st.session_state._engine


# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------

def sidebar_config(cfg: dict) -> dict:
    """Config-Editor in der Sidebar."""
    st.sidebar.markdown("### Konfiguration")
    with st.sidebar.expander("Pfade bearbeiten", expanded=False):
        cfg["dlna"]["output_dir"] = st.text_input(
            "DLNA-Ausgabeordner", value=cfg["dlna"]["output_dir"]
        )
        cfg["syncthing"]["sync_dir"] = st.text_input(
            "Syncthing-Ordner", value=cfg["syncthing"]["sync_dir"]
        )
        cfg["digikam"]["db_path"] = st.text_input(
            "digiKam-DB", value=cfg["digikam"]["db_path"]
        )
        cfg["digikam"]["image_root"] = st.text_input(
            "Bild-Root", value=cfg["digikam"]["image_root"]
        )
        if st.button("Config speichern"):
            save_config(cfg)
            st.success("Gespeichert!")
    return cfg


def main():
    st.set_page_config(
        page_title="Foto-Suche",
        page_icon=":material/search:",
        layout="wide",
    )
    st.title("Foto-Suche")

    cfg = load_config()
    cfg = sidebar_config(cfg)

    init_state()
    engine = get_engine()

    # Index-Statistiken
    try:
        s = engine.stats()
        col_stat = st.columns(4)
        col_stat[0].metric("Bilder (Index)", s["total_images"])
        col_stat[1].metric("Mit Caption", s["with_caption"])
        col_stat[2].metric("Embedded", s["embedded"])
        col_stat[3].metric("Tags", s["tags"])
    except Exception as e:
        st.warning(f"Index-DB nicht erreichbar: {e}.  Bitte erst `python -m app.index` ausführen.")

    st.divider()

    # -- Suchleiste --
    query = st.text_input(
        "Suche (semantisch über Captions)",
        value="",
        placeholder="z.B. Sonnenuntergang am Strand mit Menschen",
    )

    # -- Filter --
    with st.expander("Filter", expanded=True):
        fcol1, fcol2, fcol3, fcol4 = st.columns(4)

        with fcol1:
            try:
                all_tags = engine.get_tags()
                tag_options = {t["tag_id"]: t["name"] for t in all_tags}
            except Exception:
                tag_options = {}
            selected_tag_names = st.multiselect(
                "Tags (Alle müssen zutreffen)",
                options=list(tag_options.values()),
            )
            # Namen zurück auf IDs mappen
            name_to_id = {v: k for k, v in tag_options.items()}
            tag_ids = [name_to_id[n] for n in selected_tag_names if n in name_to_id]

        with fcol2:
            min_rating = st.slider("Mindest-Bewertung (Sterne)", 0, 5, 0)

        with fcol3:
            col_df, col_dt = st.columns(2)
            date_from = col_df.date_input("Datum von", value=None)
            date_to = col_dt.date_input("Datum bis", value=None)

        with fcol4:
            st.caption("Geo-Box (Breite/Länge)")
            geo_lat_min = st.number_input("Lat Min", value=0.0, step=0.1, key="lat_min")
            geo_lat_max = st.number_input("Lat Max", value=0.0, step=0.1, key="lat_max")
            geo_lon_min = st.number_input("Lon Min", value=0.0, step=0.1, key="lon_min")
            geo_lon_max = st.number_input("Lon Max", value=0.0, step=0.1, key="lon_max")
            use_geo = st.checkbox("Geo-Filter aktiv", value=False)

    limit = st.slider("Max. Treffer", 10, 500, 100, step=10)

    if st.button("Suchen", type="primary"):
        geo_box = None
        if use_geo:
            geo_box = (geo_lat_min, geo_lon_min, geo_lat_max, geo_lon_max)

        date_from_str = date_from.isoformat() if date_from else None
        date_to_str = date_to.isoformat() if date_to else None

        with st.spinner("Suche läuft …"):
            try:
                hits = engine.search(
                    query=query,
                    tag_ids=tag_ids,
                    min_rating=min_rating,
                    date_from=date_from_str,
                    date_to=date_to_str,
                    geo_box=geo_box,
                    limit=limit,
                )
                st.session_state.last_results = hits
            except Exception as e:
                st.error(f"Suche fehlgeschlagen: {e}")
                st.session_state.last_results = []

    # -- Ergebnisansicht --
    hits = st.session_state.last_results
    if hits:
        st.subheader(f"{len(hits)} Treffer")

        # Buttons
        bcol1, bcol2, bcol3 = st.columns([1, 1, 3])
        with bcol1:
            if st.button("Treffer an Beamer", help="Hardlinks aller Treffer im DLNA-Ordner"):
                paths = [h.path for h in hits if os.path.exists(h.path)]
                result = dlna_push(paths, cfg)
                if result["method"] == "copy":
                    st.warning(result["message"])
                else:
                    st.success(result["message"])

        with bcol2:
            selected_hits = [h for h in hits if h.image_id in st.session_state.selected]
            n_sel = len(selected_hits)
            if st.button(f"Auswahl → Homelab ({n_sel})", disabled=(n_sel == 0)):
                pairs = [(h.path, h.taken) for h in selected_hits]
                result = sync_push(pairs, cfg)
                st.success(result["message"])

        st.caption(
            f"Ausgewählt: {len(st.session_state.selected)} Bilder. "
            "Häkchen setzen, dann 'Auswahl → Homelab' klicken."
        )

        # Thumbnail-Grid
        thumbs_root = str(Path(__file__).resolve().parent.parent / cfg["index"]["thumbs_dir"])
        cols_per_row = 5
        for i in range(0, len(hits), cols_per_row):
            row_hits = hits[i:i + cols_per_row]
            cols = st.columns(cols_per_row)
            for col, hit in zip(cols, row_hits):
                with col:
                    is_selected = hit.image_id in st.session_state.selected
                    cb_key = f"cb_{hit.image_id}"
                    checked = st.checkbox(
                        "Auswählen",
                        value=is_selected,
                        key=cb_key,
                        label_visibility="collapsed",
                    )
                    if checked:
                        st.session_state.selected.add(hit.image_id)
                    elif hit.image_id in st.session_state.selected:
                        st.session_state.selected.discard(hit.image_id)

                    tb = _thumb_bytes(hit.path, 200, thumbs_root)
                    if tb:
                        st.image(tb, use_container_width=True)
                    else:
                        st.markdown("*Kein Vorschaubild*")

                    # Metadaten
                    stars = "★" * max(hit.rating or 0, 0) if hit.rating and hit.rating > 0 else ""
                    date_str = (hit.taken or "")[:10]
                    caption_snippet = (hit.caption or "")[:80]
                    if len(hit.caption or "") > 80:
                        caption_snippet += "…"

                    score_str = f" | Score: {hit.score:.3f}" if hit.score is not None else ""
                    st.caption(f"{date_str} {stars}{score_str}")
                    if caption_snippet:
                        st.caption(caption_snippet)
                    if hit.tags:
                        st.caption(", ".join(hit.tags[:5]))
    elif hits is not None and len(hits) == 0:
        st.info("Keine Treffer.  Suche anpassen oder `python -m app.index` ausführen, "
                "um den Index zu initialisieren.")

    engine.close()


if __name__ == "__main__":
    main()
