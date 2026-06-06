# CVAT Dev Tools

Skripte und Hilfsprogramme für Betrieb, Backup und Preprocessing rund um diese CVAT-Instanz.

---

## Inhalt

| Datei | Zweck |
|---|---|
| `preprocess_video.py` | Frame-Differenz-Pipeline: filtert redundante Frames aus MP4-Videos |
| `requirements_preprocess.txt` | Python-Abhängigkeiten für `preprocess_video.py` |
| `backup_cvat.sh` | Erstellt ein Backup (DB, Daten, Events) |
| `rollback_cvat.sh` | Spielt ein Backup ein + Health-Check mit Auto-Restart |
| `safe_rollback_cvat.sh` | Rollback mit zusätzlicher Sicherheitsabfrage |
| `backup_projects_via_api.py` | Projekt-Export via CVAT REST API |

---

## preprocess_video.py

### Was es macht

Die Pipeline misst die Frame-zu-Frame-Ähnlichkeit und verwirft redundante Frames — d. h. Frames, die sich kaum vom zuletzt behaltenen Frame unterscheiden. Das reduziert die Datenmenge vor dem Upload in CVAT deutlich.

**Verfügbare Metriken:**

| Metrik | Beschreibung | Schwellwert-Logik |
|---|---|---|
| `ssim` (Standard) | Structural Similarity Index (0–1, 1 = identisch) | Frame behalten wenn `SSIM < threshold` |
| `mse` | Mean Squared Error (normiert auf 0–1) | Frame behalten wenn `MSE > threshold` |

**Threshold-Modi:**

- **Dynamisch** (Standard): Zwei-Phasen-Ansatz — erst alle aufeinanderfolgenden Frame-Scores berechnen, dann Schwellwert aus der Verteilung ableiten (`mean - 0.5 × std` für SSIM). Konservativ eingestellt — behält auch bei dynamischen Videos genug Frames.
- **Statisch** (`--no-dynamic`): fester Wert, z. B. `--no-dynamic --threshold 0.98`

### Installation

```bash
pip install -r dev/requirements_preprocess.txt
```

Benötigt: `opencv-python`, `numpy`, `requests`

### Verwendung

```bash
# Standard: dynamischer Threshold, direkt in CVAT hochladen (kein Output-File nötig)
python dev/preprocess_video.py input.mp4 \
    --upload-to-cvat \
    --cvat-host http://10.28.252.47:8080 \
    --cvat-user admin \
    --cvat-pass geheimespasswort \
    --cvat-project 1

# Mit Motorhaube-Maske (untere 15% geschwärzt)
python dev/preprocess_video.py input.mp4 \
    --mask-hood 0.15 \
    --upload-to-cvat --cvat-host http://10.28.252.47:8080 \
    --cvat-user admin --cvat-pass geheimespasswort --cvat-project 1

# Preprocessing überspringen, Original direkt hochladen
python dev/preprocess_video.py input.mp4 \
    --no-filter \
    --upload-to-cvat --cvat-host http://10.28.252.47:8080 \
    --cvat-user admin --cvat-pass geheimespasswort --cvat-project 1

# Nur filtern, Output-Datei speichern (ohne Upload)
python dev/preprocess_video.py input.mp4 output.mp4 --mask-hood 0.15

# Fester Threshold statt dynamisch
python dev/preprocess_video.py input.mp4 output.mp4 --no-dynamic --threshold 0.95
```

### Alle Optionen

```
positional:
  input                   Eingabe-MP4
  output                  Ausgabe-MP4 (optional wenn --upload-to-cvat gesetzt)

Filtering:
  --no-filter             Preprocessing komplett überspringen, Original verwenden
  --metric {ssim,mse}     Ähnlichkeitsmetrik (Standard: ssim)
  --threshold FLOAT       Schwellwert (Standard: 0.98 für SSIM, 0.001 für MSE)
  --no-dynamic            Festen Threshold verwenden statt automatisch ableiten
  --mask-hood RATIO       Untere RATIO-Fraktion jedes Frames schwärzen (0.0–1.0)

CVAT-Upload (optional):
  --upload-to-cvat        Nach dem Filtern direkt in CVAT hochladen
  --cvat-host URL         CVAT-Adresse (Standard: http://localhost:8080)
  --cvat-user NAME        CVAT-Benutzername (Standard: admin)
  --cvat-pass PASS        CVAT-Passwort
  --cvat-project INT      Ziel-Projekt-ID in CVAT (Standard: 1)
  --task-name NAME        Name des neuen Tasks (Standard: Input-Dateiname)
```

### Beispielausgabe

```
Input:  kamera.mp4  (3600 frames, 25.00 fps, 1920x1080)
Dynamic threshold derived from score stats (mean=0.9821, std=0.0143): 0.9607
Filtering with metric=ssim, threshold=0.9607 ...
  Processing: 3600/3600 frames (100.0%)
Output: kamera_filtered.mp4  (612/3600 frames kept, 17.0%)
Created CVAT task: id=42, name='Kamera-Ost 2024-06-07'
Uploaded video to task 42. Upload status: 202
Task URL: http://10.28.252.47:8080/tasks/42
```

### Hinweise

- Der Standard-Threshold `0.98` (SSIM) ist konservativ — es werden mehr Frames behalten, dafür gehen weniger Bewegungsmomente verloren.
- Für aggressiveres Filtern (z. B. sehr statische Kameras): `--threshold 0.95` oder `--no-dynamic --threshold 0.95`.
- Der dynamische Modus ist für bewegungsreiche Videos immer empfohlen und standardmäßig aktiv.
- **Preprocessing an/ausschalten:** `--no-filter` überspringt die gesamte Filterlogik und lädt das Original hoch.
- Sicherheitsnetz: Würden weniger als 5 % der Frames behalten, kopiert das Skript das Original unverändert und gibt eine Warnung aus.

---

## rollback_cvat.sh

Spielt ein Backup in die laufende CVAT-Instanz ein. Stoppt alle Container, stellt DB, Daten und Events wieder her, startet danach alles neu.

### Verwendung

```bash
# Neuestes Backup automatisch verwenden
bash dev/rollback_cvat.sh

# Bestimmtes Backup-Verzeichnis angeben
bash dev/rollback_cvat.sh backups/cvat_20240607_040000
```

### Ablauf

1. Container stoppen (`docker compose stop`)
2. Backup-Archive einspielen (DB, Daten, Events)
3. Container starten (`docker compose up -d`)
4. **Health-Check**: 60 Sekunden warten, dann alle kritischen Container prüfen
5. Fehlerhafte Container werden bis zu 3× automatisch neu gestartet
6. Erfolg oder Fehlermeldung mit Exit-Code

### Geprüfte Container

| Container | Rolle |
|---|---|
| `cvat_db` | PostgreSQL-Datenbank |
| `cvat_redis_inmem` | Redis (In-Memory-Cache) |
| `cvat_redis_ondisk` | Redis (persistente Queue) |
| `cvat_server` | Django-Backend |
| `cvat_ui` | React-Frontend |
| `cvat_opa` | Open Policy Agent (Zugriffskontrolle) |
| `cvat_clickhouse` | Event-Datenbank |
| `cvat_worker_annotation` | Annotations-Worker |

### Exit-Codes

| Code | Bedeutung |
|---|---|
| `0` | Rollback und alle Health-Checks erfolgreich |
| `1` | Backup-Verzeichnis oder -Dateien fehlen |
| `2` | Mindestens ein Container konnte nicht wiederhergestellt werden |

Bei Exit-Code `2`:

```bash
docker compose -f docker-compose.yml -f components/serverless/docker-compose.serverless.yml \
    logs --tail=50 cvat_server
```

---

## backup_cvat.sh

Erstellt ein vollständiges Backup aller persistenten CVAT-Volumes.

```bash
bash dev/backup_cvat.sh
```

Das Backup landet unter `backups/cvat_<timestamp>/` mit den Archiven:
- `cvat_db.tar.gz` — PostgreSQL-Daten
- `cvat_data.tar.gz` — Mediendateien und Annotations
- `cvat_events_db.tar.gz` — ClickHouse-Events

---

## Umgebungsvariablen

| Variable | Standard | Beschreibung |
|---|---|---|
| `CVAT_HOST` | `10.28.252.47` | Hostname/IP der CVAT-Instanz |
| `COMPOSE_FILES` | `-f docker-compose.yml -f components/serverless/docker-compose.serverless.yml` | Docker-Compose-Dateien |
