# RTMDet Traffic Detector — Nuclio Serverless Function

Semi-automatische Annotation in CVAT via Nuclio.  
Modell: **RTMDet-tiny**, trainiert mit **MMDetection (OpenMMLab)**, Epoch 200.

---

## Inhalt dieses Verzeichnisses

| Datei | Beschreibung |
|---|---|
| `function.yaml` | Nuclio-Funktionsdefinition (Labels, Docker-Build, Trigger) |
| `main.py` | Handler: lädt Modell, verarbeitet Anfragen, gibt CVAT-JSON zurück |
| `epoch_200_ema.pth` | Inference-Checkpoint mit EMA-Gewichten (konvertiert aus `epoch_200.pth`) |

Der originale Checkpoint (`epoch_200.pth`) liegt im CVAT-Root-Verzeichnis.  
Die Konvertierung erfolgte mit `convert_checkpoint.py` (ein Verzeichnis höher).

---

## Modelldetails

| Eigenschaft | Wert |
|---|---|
| Architektur | RTMDet-tiny (CSPNeXt-Backbone + RTMDet-Head) |
| Framework | MMDetection 3.3.0 / mmengine 0.10.7 / mmcv 2.1.0 |
| PyTorch | 2.1.2 (CPU) |
| NumPy | < 2.0 (fest gepinnt — PyTorch 2.1.x inkompatibel mit NumPy 2.x) |
| Eingabegröße | 640 × 640 px (Letterbox-Padding) |
| Trainings-Epochen | 200 |
| Gewichte | EMA (Exponential Moving Average) |

### Klassen (9)

| ID | Name |
|---|---|
| 0 | pit_in |
| 1 | pit_out |
| 2 | park_parallel |
| 3 | car |
| 4 | trafficlight_red |
| 5 | park_cross |
| 6 | overtaking_permitted |
| 7 | overtaking_prohibited |
| 8 | trafficlight_green |

> **Hinweis:** Das Modell wurde mit 9 Klassen trainiert. Die ursprünglich geplanten 14 Klassen
> (`trafficlight_yellow`, `park_uncertain`, `pit_uncertain`, etc.) sind nicht im Checkpoint
> enthalten — sie wurden in einem späteren Trainingslauf hinzugefügt.

---

## Warum EMA-Gewichte?

MMDetection speichert während des Trainings zwei Gewichtssätze:

- **`state_dict`** — die aktuellen Trainingsgewichte nach jedem Schritt
- **`ema_state_dict`** — das gleitende Mittel der Gewichte über die letzten Epochen

EMA-Gewichte sind geglättet und generalisieren in der Regel besser auf neue Daten
(typisch +0.5–1.5 mAP). `convert_checkpoint.py` extrahiert sie und speichert
sie als `epoch_200_ema.pth`, damit MMDetection sie direkt laden kann.

---

## Funktionsweise (Anfrage → Antwort)

```
CVAT                         Nuclio-Container
  │                               │
  │── POST /  ──────────────────► │
  │   { "image": "<base64>",      │
  │     "threshold": 0.5 }        │
  │                               │  base64 → PIL.Image → numpy array
  │                               │  inference_detector(model, image)
  │                               │  Boxes filtern nach confidence ≥ threshold
  │                               │  Koordinaten auf Bildgrenzen clippen
  │◄── JSON ──────────────────────│
      [
        {
          "confidence": "0.923",
          "label": "car",
          "points": [x1, y1, x2, y2],
          "type": "rectangle"
        },
        ...
      ]
```

### `init_context` (einmalig beim Start)

1. Labels aus `function.yaml` lesen → `{id: name}` Dict
2. `epoch_200_ema.pth` laden → MMDetection-Config aus `ckpt["meta"]["cfg"]` extrahieren
3. Config in temporäre `.py`-Datei schreiben (wird nach dem Laden wieder gelöscht)
4. `init_detector(config, checkpoint, device="cpu")` → Modell auf CPU

### `handler` (pro Anfrage)

1. Base64-Bild dekodieren → RGB NumPy-Array
2. `inference_detector(model, image)` — RTMDet übernimmt Resizing intern (640 × 640)
3. Ergebnisse filtern: `score ≥ threshold`
4. Koordinaten als `[x1, y1, x2, y2]` (Integer, geclippt auf Bildgrenzen) zurückgeben

**Kaltstart:** Der erste Request nach dem Container-Start dauert ~12 Sekunden (MMDetection
lädt das Modell lazy). Alle folgenden Requests brauchen ~3 Sekunden pro Frame.

---

## Voraussetzungen (Einmalig)

### 1. CVAT_HOST setzen

Im CVAT-Root-Verzeichnis muss eine `.env`-Datei mit der Server-IP existieren:

```bash
# /home/devbox/Documents/cvat/.env
CVAT_HOST=10.28.252.47
```

Ohne diese Datei setzt Traefik den Host auf `localhost` und CVAT ist von außen
nicht erreichbar.

### 2. nuctl installieren

`nuctl` muss exakt zur Nuclio-Dashboard-Version aus `docker-compose.serverless.yml` passen
(aktuell **1.15.9**):

```bash
curl -L https://github.com/nuclio/nuclio/releases/download/1.15.9/nuctl-1.15.9-linux-amd64 \
  -o /tmp/nuctl && chmod +x /tmp/nuctl && sudo mv /tmp/nuctl /usr/local/bin/nuctl

nuctl version
```

---

## CVAT + Nuclio starten

**Immer beide Compose-Dateien angeben** — nur so bekommt `cvat_server` die nötigen
Einstellungen (`CVAT_SERVERLESS=1`, `host.docker.internal`-Auflösung):

```bash
# Im CVAT-Root-Verzeichnis ausführen
docker compose \
  -f docker-compose.yml \
  -f components/serverless/docker-compose.serverless.yml \
  up -d
```

> **Achtung:** Wird `cvat_server` ohne das Serverless-Overlay gestartet, fehlen
> `CVAT_SERVERLESS=1` und `host.docker.internal:host-gateway`. CVAT kann dann die
> Nuclio-Funktion nicht erreichen (Fehlermeldung: `NameResolutionError`).

---

## Funktion deployen (Erstmalig)

```bash
# Im CVAT-Root-Verzeichnis ausführen
nuctl create project cvat --platform local 2>/dev/null || true

nuctl deploy --project-name cvat \
  --path serverless/pytorch/custom/yolo-traffic/nuclio \
  --platform local \
  --platform-config '{"attributes": {"network": "cvat_cvat"}}' \
  --env CVAT_FUNCTIONS_REDIS_HOST=cvat_redis_ondisk \
  --env CVAT_FUNCTIONS_REDIS_PORT=6666
```

Erfolgreich, wenn die Ausgabe endet mit:
```
Function deploy complete {"functionName": "pytorch-custom-rtmdet-traffic", "httpPort": ...}
```

---

## Funktion aktualisieren / neu deployen

Wenn `main.py` oder `function.yaml` geändert wurde:

```bash
nuctl deploy --project-name cvat \
  --path serverless/pytorch/custom/yolo-traffic/nuclio \
  --platform local \
  --platform-config '{"attributes": {"network": "cvat_cvat"}}' \
  --env CVAT_FUNCTIONS_REDIS_HOST=cvat_redis_ondisk \
  --env CVAT_FUNCTIONS_REDIS_PORT=6666
```

Wenn **nur `main.py`** geändert wurde (kein Dockerfile-Rebuild nötig):

```bash
# Laufenden Container direkt aktualisieren — deutlich schneller
CONTAINER=$(docker ps --filter "name=nuclio-pytorch-custom-rtmdet-traffic" --format "{{.ID}}")
docker cp main.py "$CONTAINER":/opt/nuclio/main.py
docker restart "$CONTAINER"
```

> **Wichtig nach `docker restart`:** Nuclio weist dem Container einen neuen Port zu.
> Das Dashboard behält den alten Port. Danach immer `nuctl deploy` ausführen um
> Port und Dashboard zu synchronisieren — sonst schlägt CVAT-Annotation mit 503 fehl.

---

## Funktion testen (ohne CVAT)

```bash
# Aktuellen Port ermitteln
docker ps --filter "name=nuclio-pytorch-custom-rtmdet-traffic" --format "{{.Ports}}"

# Testbild als Base64 kodieren und anfragen (Port ggf. anpassen)
IMAGE_B64=$(base64 -w 0 /pfad/zu/testbild.jpg)

curl -s -X POST http://localhost:32769 \
  -H "Content-Type: application/json" \
  -d "{\"image\": \"$IMAGE_B64\", \"threshold\": 0.4}" | python3 -m json.tool
```

Erwartete Antwort:
```json
[
  {
    "confidence": "0.872364",
    "label": "car",
    "points": [142, 87, 398, 231],
    "type": "rectangle"
  }
]
```

Leere Liste `[]` = keine Detektionen über dem Schwellwert — kein Fehler.

---

## In CVAT verwenden

### Alle Frames eines Videos annotieren (richtig)

Für **automatische Annotation aller Frames** immer über das Menü gehen —
nicht über den Einzelbild-Button im AI-Tools-Panel.

**Variante A — aus dem Annotationseditor:**
```
Menü oben links (☰) → "Automatic annotation"
  → Funktion: RTMDet Traffic Detector
  → Threshold einstellen
  → Submit
```

**Variante B — aus der Job-Liste:**
```
Tasks → Job auswählen → ⋮ (drei Punkte) → "Automatic annotation"
  → Funktion: RTMDet Traffic Detector
  → Submit
```

Den Fortschritt sieht man in der Job-Liste. Der Worker verarbeitet alle Frames
sequenziell (~3 Sek./Frame nach dem Kaltstart).

### Einzelnen Frame annotieren (AI Tools)

```
Annotationseditor → AI Tools (rechtes Panel) → Detectors
  → RTMDet Traffic Detector
  → Annotate
```

> Dieser Button annotiert **nur den aktuell sichtbaren Frame** — nicht das gesamte Video.

---

## Status prüfen / laufende Funktionen anzeigen

```bash
nuctl get function --platform local
```

---

## Funktion entfernen

```bash
nuctl delete function pytorch-custom-rtmdet-traffic --platform local
```

---

## Checkpoint neu konvertieren (z. B. nach erneutem Training)

```bash
# Im Verzeichnis serverless/pytorch/custom/yolo-traffic/
python3 convert_checkpoint.py /pfad/zum/neuen/epoch_XYZ.pth \
  --output nuclio/epoch_200_ema.pth
```

Danach Funktion neu deployen (siehe oben).

---

## Bekannte Probleme & Lösungen

| Problem | Ursache | Lösung |
|---|---|---|
| `NameResolutionError: host.docker.internal` | `cvat_server` ohne Serverless-Overlay gestartet | `docker compose up -d` mit **beiden** `-f`-Flags neu starten |
| `405 Not Allowed` (nginx) bei Annotation | Traefik routet POST auf `/api/` zur UI statt zum Server | `cvat_server` mit korrektem `CVAT_HOST` neu erstellen (Priority-Label wird neu gesetzt) |
| `RuntimeError: Numpy is not available` | NumPy 2.x inkompatibel mit PyTorch 2.1.x | In `function.yaml` ist `numpy<2.0` gepinnt — nur nach Rebuild relevant |
| `503` beim ersten Frame | Kaltstart ~12 Sek. überschreitet Client-Timeout | `eventTimeout: 120s` in `function.yaml` gesetzt |
| CVAT nach Neustart nicht erreichbar | `CVAT_HOST` fehlt → Traefik-Regel auf `localhost` | `.env`-Datei mit `CVAT_HOST=10.28.252.47` im CVAT-Root anlegen |
| Nur erster Frame annotiert | Falscher Annotierungsweg (AI-Tools statt Auto-Annotation) | Menü → "Automatic annotation" verwenden |
| Port-Mismatch nach `docker restart` | Nuclio vergibt neuen Port, Dashboard weiß es nicht | `nuctl deploy` ausführen um Port zu synchronisieren |

---

## Bekannte Einschränkungen

- **Nur CPU** — die aktuelle Konfiguration nutzt kein GPU. Für GPU-Inferenz
  `function-gpu.yaml` anlegen und `device="cpu"` in `main.py` auf `"cuda:0"` ändern.
- **Bildgröße** — Requests sind auf 32 MB begrenzt (`maxRequestBodySize`).
  Für sehr hochauflösende Bilder ggf. auf 67108864 (64 MB) erhöhen.
