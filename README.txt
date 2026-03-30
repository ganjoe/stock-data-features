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

  2. ON-THE-FLY:    Berechnet einzelne Indikatoren für einzelne Ticker in
                    Echtzeit und gibt das Ergebnis direkt als JSON zurück.
                    Aktuell unterstützt:
                      - Moving Averages (SMA, EMA)    via POST /features/ma
                      - RS Rating (IBD-Methode)       via POST /features/rs


Programmablauf
--------------
  Docker Start
       │
       ▼
  FastAPI startet auf Port 8003
       │
       ├── Wartet auf POST /features/calculate    (Batch von stock-data-node)
       ├── Wartet auf POST /features/ma           (On-the-Fly MA)
       ├── Wartet auf POST /features/rs           (On-the-Fly RS Rating)
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

Bei einem On-the-Fly RS-Rating-Aufruf (/features/rs):
  Modus A — vs Benchmark (z.B. "AAPL vs SPX"):
    1. OHLCV-Daten für Ticker und Benchmark werden geladen
    2. Für beide wird der gewichtete ROC-Score berechnet (IBD-Methode):
       raw = (2 × ROC_63) + ROC_126 + ROC_189 + ROC_252
    3. Die Differenz (Ticker - Benchmark) wird zurückgegeben
       → Positiv = Ticker outperformt, Negativ = Benchmark stärker

  Modus B — vs alle Ticker (kein Benchmark angegeben):
    1. OHLCV-Daten für ALLE Ticker im Parquet-Verzeichnis werden geladen
    2. Für jeden Ticker wird der gewichtete ROC-Score berechnet
    3. Der angefragte Ticker wird im Cross-Sectional-Ranking eingestuft
    4. Ein Perzentilwert (1-99) wird zurückgegeben
       → 99 = stärkster Ticker, 1 = schwächster


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


POST /features/rs
------------------
Berechnet on-the-fly das IBD RS Rating für einen einzelnen Ticker.
Zwei Modi je nachdem ob ein Benchmark angegeben wird.

  Request Body (JSON):
    {
      "ticker":           "AAPL",         (string, Pflicht)  Ticker-Symbol
      "benchmark":        "SPX",          (string, Optional) Benchmark-Ticker, Default: null
      "chart_timeframe":  "1D"            (string, Optional) Quell-Timeframe, Default: "1D"
    }

  Modus A — Mit Benchmark (Response 200 OK):
    {
      "ticker":               "AAPL",
      "benchmark":            "SPX",
      "mode":                 "vs_benchmark",
      "ticker_raw_score":     185.4321,
      "benchmark_raw_score":  120.1234,
      "rs_relative":          65.3087,
      "interpretation":       "positive = ticker outperforms benchmark"
    }

  Modus B — Ohne Benchmark = vs alle Ticker (Response 200 OK):
    {
      "ticker":               "AAPL",
      "benchmark":            null,
      "mode":                 "vs_all",
      "rs_rating":            87,
      "raw_score":            185.4321,
      "universe_size":        1450,
      "interpretation":       "Outperforms 87% of 1450 tickers"
    }

  Response (404 Not Found):
    {
      "error": "No data found for ticker 'XYZ'"
    }
    oder:
    {
      "error": "No data found for benchmark 'INVALID'"
    }

  Response (400 Bad Request):
    {
      "error": "Not enough data for 'XYZ' (need >= 63 rows, have 10)"
    }

  Beispiele:

    # RS Rating von AAPL vs alle Ticker (Cross-Sectional Rank)
    curl -X POST http://localhost:8003/features/rs \
      -H "Content-Type: application/json" \
      -d '{"ticker": "AAPL"}'

    # RS Rating von AAPL vs SPX (Benchmark-Vergleich)
    curl -X POST http://localhost:8003/features/rs \
      -H "Content-Type: application/json" \
      -d '{"ticker": "AAPL", "benchmark": "SPX"}'

    # RS Rating von MSFT vs QQQ
    curl -X POST http://localhost:8003/features/rs \
      -H "Content-Type: application/json" \
      -d '{"ticker": "MSFT", "benchmark": "QQQ"}'


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

Das On-the-Fly Interface ist aktuell auf Moving Averages und RS Rating
beschränkt. Geplante Erweiterungen:

  - POST /features/bollinger   → Bollinger Bänder
  - POST /features/stochastic  → Stochastik %K / %D
  - POST /features/rsi         → Relative Strength Index

Die bestehende calculator.py enthält bereits die Berechnungslogik für
Bollinger und Stochastic — die Endpunkte müssen nur analog zu /features/ma
implementiert werden.
