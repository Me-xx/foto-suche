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
# Vollbild-Dialog (Original + Browser-Download)
# ---------------------------------------------------------------------------

_MIME_MAP = {
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
    ".webp": "image/webp", ".bmp": "image/bmp", ".tif": "image/tiff",
    ".tiff": "image/tiff", ".heic": "image/heic", ".gif": "image/gif",
    ".mp4": "video/mp4", ".mov": "video/quicktime",
}


def _mime_for(name: str) -> str:
    return _MIME_MAP.get(os.path.splitext(name)[1].lower(), "application/octet-stream")


@st.dialog("Vollbild", width="large")
def _full_dialog(path: str, name: str):
    st.image(path)
    try:
        data = Path(path).read_bytes()
    except OSError:
        st.error(f"Datei nicht lesbar: {path}")
        return
    st.download_button(
        "Original herunterladen",
        data=data,
        file_name=name,
        mime=_mime_for(name),
        type="primary",
        use_container_width=True,
    )
    st.caption(path)


# ---------------------------------------------------------------------------
# Session-State
# ---------------------------------------------------------------------------

def init_state():
    if "last_results" not in st.session_state:
        st.session_state.last_results = []
    if "cart" not in st.session_state:
        # Warenkorb: image_id -> SearchHit; überlebt neue Suchen/Filter
        st.session_state.cart = {}


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


def sidebar_video_config() -> dict:
    """Diashow-/Video-Einstellungen in der Sidebar (MP3-Soundtrack + Zeit)."""
    import tempfile as _tempfile

    with st.sidebar.expander("Diashow (Video)", expanded=False):
        uploaded = st.file_uploader(
            "MP3-Soundtrack (optional)", type=["mp3", "wav"]
        )
        mp3_path = None
        if uploaded is not None:
            sig = f"{uploaded.name}_{uploaded.size}"
            if st.session_state.get("_video_mp3_sig") != sig:
                p = Path(_tempfile.gettempdir()) / f"foto_suche_soundtrack_{uploaded.name}"
                with open(p, "wb") as f:
                    f.write(uploaded.getvalue())
                st.session_state._video_mp3_sig = sig
                st.session_state._video_mp3_path = str(p)
            mp3_path = st.session_state._video_mp3_path
            st.caption(f"Aktives Lied: {uploaded.name}")
        else:
            st.session_state._video_mp3_path = None

        seconds = st.number_input(
            "Sekunden pro Bild", min_value=0.5, max_value=30.0, value=4.0, step=0.5
        )
        fade = st.number_input(
            "Überblendung (Sekunden)", min_value=0.0, max_value=5.0, value=1.0, step=0.5
        )
    return {"mp3_path": mp3_path, "seconds": seconds, "fade": fade}


def sidebar_cart(cfg: dict, video_cfg: dict) -> None:
    """Warenkorb 'Gewählte Bilder' in der Sidebar mit Homelab-Buttons."""
    cart = st.session_state.cart
    thumbs_root = str(
        Path(__file__).resolve().parent.parent / cfg["index"]["thumbs_dir"]
    )

    with st.sidebar.expander(f"Gewählte Bilder ({len(cart)})", expanded=False):
        if not cart:
            st.caption(
                "Noch keine Bilder gewählt — Häkchen im Ergebnis-Grid setzen "
                "und '→ Gewählte Bilder' klicken. Filter danach beliebig "
                "ändern und weitere Bilder hinzufügen."
            )
            return

        # Nach Aufnahmedatum sortierte Liste, pro Zeile: Thumb | Name | Mülleimer
        ordered = sorted(
            cart.items(), key=lambda kv: ((kv[1].taken or ""), kv[1].name or "")
        )
        for iid, h in ordered:
            r_thumb, r_name, r_dl, r_del = st.columns([0.12, 0.62, 0.13, 0.13])
            tb = _thumb_bytes(h.path, 100, thumbs_root)
            if tb:
                r_thumb.image(tb, width=42)
            else:
                r_thumb.caption("—")
            r_name.caption(f"{(h.taken or '????')[:10]}  {h.name}")
            if r_dl.button(
                ":material/open_in_full:", key=f"cart_dl_{iid}",
                help="Vollbild öffnen und Original herunterladen",
            ):
                _full_dialog(h.path, h.name)
            if r_del.button(
                ":material/delete:", key=f"cart_del_{iid}",
                help="Aus der Auswahl entfernen",
            ):
                st.session_state.cart.pop(iid, None)
                print(f"[debug] Warenkorb: Bild {iid} entfernt, noch {len(st.session_state.cart)}")
                st.rerun()

        if st.button(":material/delete_sweep: Alle entfernen"):
            print(f"[debug] Warenkorb: alle {len(st.session_state.cart)} Einträge entfernt")
            st.session_state.cart = {}
            st.rerun()

        st.divider()

        n = len(st.session_state.cart)
        if st.button(
            f"Auswahl → Homelab Pics ({n})",
            type="primary",
            help="Bilder aus dem Warenkorb in den Syncthing-Ordner kopieren",
        ):
            pairs = [(h.path, h.taken) for h in st.session_state.cart.values()]
            print(f"[debug] Homelab Pics: übertrage {len(pairs)} Bilder:")
            for p, t in pairs:
                print(f"[debug]   {t or '????'}  {p}")
            result = sync_push(pairs, cfg)
            print(
                f"[debug] Homelab Pics Ergebnis: "
                f"{'FEHLER' if result.get('errors') else 'ok'} | {result.get('message')}"
            )
            if result.get("errors"):
                st.error(result["message"])
            else:
                st.success(result["message"])

        if st.button(
            f"Auswahl → Homelab Video ({n})",
            help="Diashow aus der Auswahl (nach Aufnahmedatum) mit optionalem "
            "MP3-Soundtrack → Syncthing-Ordner Videos",
        ):
            from app.video import (
                create_slideshow,
                push_video_to_syncthing,
                cleanup_temp,
            )

            cart_hits = list(st.session_state.cart.values())
            specs = [(h.path, h.taken) for h in cart_hits]
            print(f"[debug] Homelab Video: {len(specs)} Bilder aus dem Warenkorb:")
            for p, t in specs:
                print(f"[debug]   {t or '????'}  {p}")
            if not specs:
                print("[debug] Homelab Video abgebrochen: Warenkorb ist leer.")
                st.info("Warenkorb ist leer — erst Bilder auswählen.")
            else:
                temp_dir = None
                try:
                    with st.spinner("Diashow wird gerendert (ffmpeg) …"):
                        res = create_slideshow(
                            specs,
                            seconds_per_image=video_cfg["seconds"],
                            fade_s=video_cfg["fade"],
                            music_path=video_cfg["mp3_path"],
                        )
                    temp_dir = res.get("temp_dir")
                    print(
                        f"[debug] Homelab Video Ergebnis: "
                        f"{'FEHLER' if not res['ok'] else 'ok'} | {res['message']}"
                    )
                    if not res["ok"]:
                        st.error(res["message"])
                    else:
                        jahr = min(
                            (h.taken for h in cart_hits if h.taken), default=None
                        )
                        pattern = cfg["syncthing"].get(
                            "video_subfolder_pattern", "Videos/Auswahl/{jahr}"
                        )
                        push = push_video_to_syncthing(
                            res["video_path"], cfg["syncthing"]["sync_dir"], pattern, jahr
                        )
                        print(f"[debug] Homelab Video Kopieren: ok={push['ok']} | {push.get('message')}")
                        if push["ok"]:
                            st.success(f"{res['message']}  {push['message']}")
                        else:
                            st.error(push.get("message", "Kopieren fehlgeschlagen."))
                finally:
                    cleanup_temp(temp_dir)


def main():
    st.set_page_config(
        page_title="Foto-Suche",
        page_icon=":material/search:",
        layout="wide",
    )
    st.title("Foto-Suche")

    cfg = load_config()
    init_state()
    cfg = sidebar_config(cfg)
    video_cfg = sidebar_video_config()
    sidebar_cart(cfg, video_cfg)

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
            st.caption("Geo-Filter")
            use_geo = st.checkbox("Geo-Filter aktiv", value=False)

    # -- Geo-Box: Ortssuche, Größe in km, interaktive Karte -----------------
    geo_box = None
    if use_geo:
        from app.geo import geocode, box_from_center, box_dimensions_km

        if "geo_center" not in st.session_state:
            st.session_state.geo_center = (49.45, 11.08)  # Default: Nürnberg
        if "geo_box_drawn" not in st.session_state:
            st.session_state.geo_box_drawn = None

        gcol1, gcol2, gcol3 = st.columns([2, 1, 1])
        with gcol1:
            ort = st.text_input(
                "Ortssuche (Mittelpunkt der Box)", placeholder="z.B. Nürnberg"
            )
            if st.button("Ort suchen") and ort.strip():
                res = geocode(ort.strip())
                if res:
                    st.session_state.geo_center = (res["lat"], res["lon"])
                    st.session_state.geo_box_drawn = None
                    st.success(f"→ {res['display_name']}")
                else:
                    st.error("Ort nicht gefunden oder Nominatim nicht erreichbar.")
        with gcol2:
            width_km = st.number_input(
                "Breite (km)", min_value=0.5, max_value=500.0, value=10.0, step=0.5
            )
        with gcol3:
            height_km = st.number_input(
                "Höhe (km)", min_value=0.5, max_value=500.0, value=10.0, step=0.5
            )

        if st.session_state.geo_box_drawn is not None:
            c_lat, c_lon, w, h = box_dimensions_km(st.session_state.geo_box_drawn)
            st.info(
                f"Box aus Karte: Mitte {c_lat:.4f}, {c_lon:.4f} — "
                f"{w:.1f} × {h:.1f} km"
            )
            if st.button("Karten-Box zurücksetzen"):
                st.session_state.geo_box_drawn = None
                st.rerun()

        # Interaktive Karte: Rechteck zeichnen/ziehen überschreibt Ort/Größe
        try:
            import folium
            from folium.plugins import Draw
            from streamlit_folium import st_folium

            center_lat, center_lon = st.session_state.geo_center
            show_box = st.session_state.geo_box_drawn or box_from_center(
                center_lat, center_lon, width_km, height_km
            )
            m = folium.Map(
                location=[center_lat, center_lon], zoom_start=11, tiles="OpenStreetMap"
            )
            lat_min, lon_min, lat_max, lon_max = show_box
            folium.Rectangle(
                bounds=[(lat_min, lon_min), (lat_max, lon_max)],
                color="#ff0000", fill=True, fill_opacity=0.15, weight=2,
            ).add_to(m)
            folium.Marker(
                location=[center_lat, center_lon],
                tooltip="Mittelpunkt", icon=folium.Icon(color="red"),
            ).add_to(m)
            Draw(
                export=False,
                draw_options={
                    "polyline": False, "polygon": False, "circle": False,
                    "marker": False, "circlemarker": False,
                },
            ).add_to(m)
            st.caption(
                "Rechteck-Werkzeug (▭) auf der Karte: Box zeichnen und ziehen — "
                "überschreibt Ort und Größe. Neu zeichnen ersetzt die Box."
            )
            map_data = st_folium(m, height=420, key="geo_map")
            drawn = (map_data or {}).get("last_active_drawing")
            if drawn and drawn.get("geometry", {}).get("type") == "Polygon":
                coords = drawn["geometry"]["coordinates"][0]
                lats = [c[1] for c in coords]
                lons = [c[0] for c in coords]
                if max(lats) > min(lats) and max(lons) > min(lons):
                    new_box = (min(lats), min(lons), max(lats), max(lons))
                    if new_box != st.session_state.geo_box_drawn:
                        st.session_state.geo_box_drawn = new_box
                        c_lat, c_lon, _, _ = box_dimensions_km(new_box)
                        st.session_state.geo_center = (c_lat, c_lon)
                        st.rerun()
        except ImportError:
            st.warning(
                "Karte nicht verfügbar: 'pip install streamlit-folium folium' "
                "ausführen."
            )

    limit = st.slider("Max. Treffer", 10, 500, 100, step=10)

    if st.button("Suchen", type="primary"):
        if use_geo:
            from app.geo import box_from_center as _bfc
            if st.session_state.get("geo_box_drawn"):
                geo_box = st.session_state.geo_box_drawn
            else:
                c_lat, c_lon = st.session_state.geo_center
                geo_box = _bfc(c_lat, c_lon, width_km, height_km)

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
        bcol1, bcol2 = st.columns([1.2, 1.8])
        with bcol1:
            if st.button("Treffer an Beamer", help="Hardlinks aller Treffer im DLNA-Ordner"):
                paths = [h.path for h in hits if os.path.exists(h.path)]
                result = dlna_push(paths, cfg)
                if result["method"] == "copy":
                    st.warning(result["message"])
                else:
                    st.success(result["message"])

        with bcol2:
            n_sel = sum(
                1 for h in hits if st.session_state.get(f"cb_{h.image_id}", False)
            )
            if st.button(
                f"Angehakte → Gewählte Bilder ({n_sel})",
                help="Häkchen ins Warenkorb-Dropdown (links) übernehmen — "
                "danach Filter ändern und weitere Bilder hinzufügen",
            ):
                # Über alle Treffer laufen und die Widget-States direkt lesen
                picked = [
                    h for h in hits if st.session_state.get(f"cb_{h.image_id}", False)
                ]
                added = [h for h in picked if h.image_id not in st.session_state.cart]
                for h in picked:
                    st.session_state.cart[h.image_id] = h
                    st.session_state.pop(f"cb_{h.image_id}", None)
                print(
                    f"[debug] Warenkorb-Übernahme: {len(hits)} Treffer, "
                    f"{len(picked)} angehakt (IDs {[h.image_id for h in picked]}), "
                    f"{len(added)} neu (IDs {[h.image_id for h in added]}), "
                    f"Warenkorb jetzt {len(st.session_state.cart)}"
                )
                st.rerun()

        picked_now = [
            h.image_id for h in hits if st.session_state.get(f"cb_{h.image_id}", False)
        ]
        print(f"[debug] Grid: {len(hits)} Treffer, Häkchen: {picked_now}")
        st.caption(
            f"Ausgewählt: {len(picked_now)} Bilder. "
            f"Warenkorb: {len(st.session_state.cart)} Bilder (Sidebar links)."
        )

        # Thumbnail-Grid
        thumbs_root = str(Path(__file__).resolve().parent.parent / cfg["index"]["thumbs_dir"])
        cols_per_row = 5
        for i in range(0, len(hits), cols_per_row):
            row_hits = hits[i:i + cols_per_row]
            cols = st.columns(cols_per_row)
            for col, hit in zip(cols, row_hits):
                with col:
                    # Häkchen liegt direkt im Widget-State (cb_<image_id>)
                    # — keine zusätzliche Spiegel-Menge mehr.
                    st.checkbox(
                        "Auswählen",
                        value=False,
                        key=f"cb_{hit.image_id}",
                        label_visibility="collapsed",
                    )

                    tb = _thumb_bytes(hit.path, 200, thumbs_root)
                    if tb:
                        st.image(tb, width="stretch")
                    else:
                        st.markdown("*Kein Vorschaubild*")

                    if st.button(
                        ":material/open_in_full:",
                        key=f"dl_{hit.image_id}",
                        help="Vollbild öffnen und Original herunterladen",
                        use_container_width=True,
                    ):
                        _full_dialog(hit.path, hit.name)

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

    # Engine bewusst NICHT schließen: sie bleibt im session_state und hält
    # den Vektoren-Cache (sonst müsste jede Suche die 40k-Embeddings neu laden).
    # Streamlit räumt beim Session-Ende auf.


if __name__ == "__main__":
    main()
