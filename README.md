# Foto-Suche MVP

Suche über die digiKam-Fotosammlung mit semantischer Vektorsuche,
DLNA-Auslieferung an einen Beamer (via Gerbera) und Syncthing-Push
ins Homelab.

## Setup

### 1. Python-Umgebung

```bash
cd foto-suche
python -m venv .venv
# Windows:
.venv\Scripts\activate
# Linux/MSYS2:
source .venv/bin/activate

pip install -r requirements.txt
```

### 2. Konfiguration

Beim ersten Start der App wird automatisch eine `config.yaml` mit
Beispielwerten erzeugt.  Pfade anpassen:

```yaml
digikam:
  db_path: "C:/MeineDateienDesk/digikam/digikam4.db"
  image_root: "C:/MeineDateienDesk/digikam"
  db_copy_path: null  # Optional: Kopie der DB, falls digiKam sperrt

embedding:
  model: "BAAI/bge-m3"
  batch_size: 16
  dimension: 1024

dlna:
  output_dir: "C:/MeineDateienDesk/dlna/suche"
  max_versions: 5

syncthing:
  sync_dir: "C:/SycnT_Server"          # Syncthing-Folder "ServerDaten" (id: daten)
  subfolder_pattern: "Bilder/Auswahl/{jahr}"
  api_url: null       # Optional: http://localhost:8384
  api_key: null       # Optional
  folder_id: "daten"  # Optional (Scan-Trigger)
```

### 3. Index aufbauen (Metadaten + Embeddings)

```bash
# Nur Metadaten + Tags synchronisieren (schnell):
python -m app.index --sync-only

# Nur Embeddings berechnen (inkrementell, abbrechbar/resumierbar):
python -m app.index --embed-only

# Alles in einem:
python -m app.index

# Statistiken:
python -m app.index --stats

# Vollständiger Rebuild:
python -m app.index --reset
```

Der Embedding-Lauf ist **inkrementell und idempotent**: nur Bilder mit
neuen/geänderten Captions werden embeddet.  Nach jedem Batch wird
committet — der Rechner kann ausgehen (Solar-Betrieb) ohne State-Verlust.
Beim erneuten Aufruf wird dort weitergemacht, wo abgebrochen wurde.

> Der erste Embedding-Lauf über ~42.000 Captions dauert auf CPU
> mehrere Stunden.  Mit GPU deutlich schneller.  Die App funktioniert
> bereits vor vollständiger Abdeckung — die Vektor-Suche greift nur
> auf bereits embedded Bilder zu.
>
> Embeddings werden als float32-BLOB direkt in der ``images``-Tabelle
> gespeichert.  Die Vektorsuche läuft zur Laufzeit über NumPy: beim ersten
> Suchaufruf werden alle Embeddings in eine (N, 1024) float32-Matrix
> geladen (~400 MB bei 100k Bildern) und per Matrixmultiplikation gegen
> die Query gerankt — exakte Cosine-Ähnlichkeit, ein Durchlauf unter
> 100 ms.  Keine sqlite-vec-Extension nötig.

### 4. App starten

```bash
streamlit run app/ui.py
```

## Gerbera-Einbindung (DLNA)

Der DLNA-Ausgabeordner (z.B. `C:\MeineDateienDesk\dlna\suche`) muss in
Gerbera als Autoscanning-Ordner konfiguriert werden.  Beispiel für die
`config.xml` von Gerbera:

```xml
<autoscan use-inotify="yes">
  <directory location="C:/MeineDateienDesk/dlna/suche" mode="inotify"
             level="full" recursive="yes"/>
</autoscan>
```

Beim Klick auf **„Treffer an Beamer"** in der App werden NTFS-Hardlinks
der Treffer im DLNA-Ordner erzeugt (kein zusätzlicher Platzverbrauch).
Gerbera erkennt die neuen Dateien per inotify automatisch.

### Voraussetzung: gleiche Partition

Hardlinks funktionieren nur auf derselben NTFS-Partition.  Wenn der
DLNA-Ordner auf einer anderen Partition liegt als die Bilder, fällt die
App automatisch auf Kopieren zurück und zeigt einen Warnhinweis.

## Suche

- **Freitext**: semantische Vektorsuche über Qwen-Captions (bge-m3).
  Bilder ohne Embedding werden per Volltext-Fallback (LIKE) gefunden.
- **Tags**: Multiselect aus digiKam-Tag-Hierarchie (AND-Semantik).
- **Bewertung**: Mindest-Sterne (0–5).
- **Datum**: Von/Bis-Datumsbereich.
- **Geo-Box**: Lat/Lon-Ecken als Zahleneingabe (keine interaktive Karte).

Alle Filter sind kombinierbar: SQL-Filter vorfiltern, dann Vektor-
Ranking auf der Teilmenge (Hybrid-Suche).

## Bedienung

1. Suchbegriff eingeben und/oder Filter setzen.
2. „Suchen" klicken → Thumbnail-Grid mit Treffern.
3. Checkboxen für Bilder anwählen.
4. **„Treffer an Beamer"**: alle Treffer als Hardlinks im DLNA-Ordner.
5. **„Auswahl → Homelab"**: angehakte Bilder in Syncthing-Ordner kopieren.

## Bekannte Einschränkungen

- Keine interaktive Karte (nur Geo-Box-Filter).
- Keine digiKam-DB-Schreibzugriffe (strikt read-only).
- Embedding-Modell wird beim ersten Aufruf heruntergeladen (~2 GB).
- Thumbnails werden extern gecacht (`thumbs/`), Original-Bilder werden
  nie verändert.

## Architektur

```
foto-suche/
  app/
    __init__.py
    config.py    # config.yaml laden/erzeugen
    digikam.py   # read-only digiKam-DB-Reader
    index.py     # sqlite-Index + Vektor-Embeddings (BLOB) (CLI)
    search.py    # Hybrid-Suche (SQL + NumPy Cosine)
    dlna.py      # Hardlink-Ergebnisordner
    sync.py      # Syncthing-Push
    ui.py        # Streamlit-Frontend
  config.yaml    # wird beim ersten Start erzeugt
  requirements.txt
  README.md
```

## Offene Konfigurationspunkte

Vor dem produktiven Einsatz in `config.yaml` klären:

1. ~~Exakter Pfad des lokalen Syncthing-Ordners~~ geklärt: `C:/SycnT_Server` (Folder „ServerDaten", id `daten`)
2. Gewünschte Ziel-Struktur für Syncthing-Push (`subfolder_pattern`)
3. GPU verfügbar? (Modellwahl/Speed — CPU reicht, ist aber langsamer)
4. digiKam-Version (Schema-Verifikation erfolgt automatisch beim Sync)
5. DLNA-Ordner auf gleicher Partition wie Bilder? (Hardlink-Bedingung)
6. Gerbera installiert? (ansonsten README-Abschnitt oben genügt)
