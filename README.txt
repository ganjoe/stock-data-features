================================================================================
  STOCK DATA FEATURES — README
================================================================================

Überblick
---------
stock-data-features ist ein eigenständiger Microservice für die Berechnung
technischer Indikatoren auf Aktien-Kursdaten. Er liest OHLCV-Daten aus
Parquet-Dateien (/data/parquet/<TICKER>/<TIMEFRAME>.parquet) und stellt
zwei Berechnungsmodi bereit:

  1. BATCH-MODUS:   Berechnet ALLE konfigurierten Features für ALLE Ticker
                    auf einmal (SMA, EMA, Bollinger, Stochastic, IBD RS,
                    Minervini Score). Wird von stock-data-node nach jedem
                    Download-Zyklus automatisch getriggert.

  2. ON-THE-FLY:    Berechnet einen einzelnen Moving Average für einen
                    einzelnen Ticker in Echtzeit und gibt das Ergebnis
                    direkt als JSON zurück. Für Dashboard-Frontends gedacht,
                    die dynamische MA-Overlays brauchen.


Programmablauf
--------------
  Docker Start
       │
       ▼
  FastAPI startet auf Port 8003
       │
       ├── Wartet auf POST /features/calculate    (Batch von stock-data-node)
       ├── Wartet auf POST /features/ma           (On-the-Fly von Frontends)
       ├── GET /health                            (Docker Healthcheck)
       └── GET /status                            (Job-Status abfragen)

Bei einem Batch-Aufruf (/features/calculate):
  1. features.json wird gelesen (fachliche Parameter: type, window, period)
  2. Alle Ticker-Ordner in /data/parquet/ werden gescannt
  3. Pro Ticker wird der OHLCV-Parquet geladen
  4. Alle Features werden parallel berechnet (ProcessPoolExecutor)
  5. Cross-sectional Features (z.B. IBD RS Ranking) werden zentral berechnet
  6. Ergebnisse werden als <timeframe>_features.parquet zurückgeschrieben

Bei einem On-the-Fly-Aufruf (/features/ma):
  1. Der OHLCV-Parquet des angegebenen Tickers wird geladen
  2. Der angeforderte Moving Average (SMA oder EMA) wird berechnet
  3. Das Ergebnis wird als JSON-Array direkt zurückgegeben (kein Schreiben)


================================================================================
  API REFERENZ
================================================================================


POST /features/calculate
------------------------
Startet die vollständige Feature-Berechnung für alle Ticker im Hintergrund.

  Request:    Kein Body erforderlich.
  
  Parameter:  ?stream=true    (optional) Gibt Echtzeit-Logs als Text-Stream zurück

  Response:   202 Accepted    — Job wurde gestartet
              409 Conflict    — Ein Job läuft bereits

  Beispiel:
    curl -X POST http://localhost:8003/features/calculate

  Beispiel (mit Echtzeit-Logs):
    curl -X POST "http://localhost:8003/features/calculate?stream=true"


POST /features/ma
------------------
Berechnet on-the-fly einen Moving Average für einen einzelnen Ticker.

  Request Body (JSON):
    {
      "ticker":           "AAPL",         (string, Pflicht)  Ticker-Symbol
      "ma_type":          "sma",          (string, Pflicht)  "sma" oder "ema"
      "chart_timeframe":  "1D",           (string, Optional) Quell-Timeframe, Default: "1D"
      "ma_window":        50              (int,    Pflicht)  MA-Periode (1-500)
    }

  Response (200 OK):
    {
      "ticker":           "AAPL",
      "chart_timeframe":  "1D",
      "ma_type":          "SMA",
      "ma_window":        50,
      "ma_label":         "sma_50",
      "data_points":      1260,
      "timestamps":       [1640000000, 1640086400, ...],
      "close":            [150.23, 151.50, ...],
      "values":           [148.50, 148.80, ...]
    }

  Response (404 Not Found):
    {
      "error": "No data found for ticker 'XYZ' with timeframe '1D'"
    }

  Response (422 Unprocessable Entity):
    Automatische Validierungsfehler bei ungültigem ma_type oder ma_window.

  Beispiele:

    # SMA 50 auf Daily-Chart für AAPL
    curl -X POST http://localhost:8003/features/ma \
      -H "Content-Type: application/json" \
      -d '{"ticker": "AAPL", "ma_type": "sma", "ma_window": 50}'

    # EMA 20 auf Daily-Chart für MSFT
    curl -X POST http://localhost:8003/features/ma \
      -H "Content-Type: application/json" \
      -d '{"ticker": "MSFT", "ma_type": "ema", "ma_window": 20}'

    # SMA 200 auf Weekly-Chart für TSLA
    curl -X POST http://localhost:8003/features/ma \
      -H "Content-Type: application/json" \
      -d '{"ticker": "TSLA", "ma_type": "sma", "chart_timeframe": "1W", "ma_window": 200}'


GET /status
-----------
Gibt zurück, ob gerade eine Batch-Berechnung läuft.

  Response:
    { "is_running": false }


GET /health
-----------
Docker Healthcheck Endpoint.

  Response:
    { "status": "ok" }


================================================================================
  KONFIGURATION
================================================================================

Umgebungsvariablen:
  APP_BASE_DIR            Basis-Verzeichnis (Default: Projektordner)
  FEATURES_API_PORT       API-Port (Default: 8003)

Shared Volumes (Docker):
  /app/config             → features.json, settings.json (von stock-data-node)
  /app/data               → Parquet-Dateien (OHLCV + Features)

Dateien:
  config/features.json    Feature-Definitionen (fachlich + visuell)
  config/settings.json    processing_threads, etc.


================================================================================
  ERWEITERBARKEIT
================================================================================

Das On-the-Fly Interface (/features/ma) ist aktuell auf Moving Averages
beschränkt. Geplante Erweiterungen:

  - POST /features/bollinger   → Bollinger Bänder
  - POST /features/stochastic  → Stochastik %K / %D
  - POST /features/rsi         → Relative Strength Index

Die bestehende calculator.py enthält bereits die Berechnungslogik für
Bollinger und Stochastic — die Endpunkte müssen nur analog zu /features/ma
implementiert werden.
