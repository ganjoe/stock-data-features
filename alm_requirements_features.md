# Requirements

| ID | Category | Title | Description | Covered By |
| :--- | :--- | :--- | :--- | :--- |
| F-API-010 | Trigger | API POST Trigger | Das System muss einen POST-Endpoint bereitstellen, über den der Feature-Berechnungsprozess manuell angestoßen werden kann. | - |
| F-API-020 | Trigger | Batch Download Trigger | Das System muss unmittelbar nach Abschluss eines regulären Download-Batches den Feature-Berechnungsprozess automatisch starten. | downloader.py, main.py (F-LC-011) |
| F-SYS-030 | Concurrency | Job Overlap Protection | Wird ein neuer Berechnungsprozess getriggert, während ein Prozess noch läuft, muss der neue Aufruf abgewiesen werden (Skip/Drop). Ein Retrigger erfolgt erst, wenn alle Features berechnet sind. | - |
| F-PRC-040 | Processing | Config Parameter Extraction | Das System muss für die Berechnung ausschließlich fachliche Parameter (z.B. `type` wie SMA/EMA, `window`, `period`) aus der Datei `features.json` extrahieren und rein optische Parameter (z.B. `color`, `thickness`, `chart_type`) ignorieren. | - |
| F-PRC-045 | Processing | Dynamic Feature Mapping | Die zu berechnenden Indikatoren müssen zur Laufzeit basierend auf den extrahierten fachlichen Parametern auf die entsprechenden Berechnungs-Algorithmen (z.B. SMA vs EMA) gemappt werden. | - |
| F-PRC-047 | Processing | Feature Implementation Scope | Die initiale Implementierung beschränkt sich auf Moving Averages (z.B. SMA, EMA). Für alle anderen in der Datei parametrisierbaren Indikatoren (z.B. Stochastik, Bollinger Bands) werden vorerst leere Funktionen/Methoden mit `pass` als Platzhalter (Stubs) angelegt. | - |
| F-SYS-050 | Concurrency | Ticker Level Parallel Execution | Das System muss die Berechnungen parallel auf Ticker-Ebene (z.B. ein Worker pro Ticker) ausführen, um Multi-Core-Prozessoren auszulasten. | - |
| F-SYS-060 | Concurrency | Configurable Thread Count | Die maximale Anzahl der parallel genutzten Threads/Worker muss konfigurierbar sein. | - |
| F-INP-070 | Storage | Parquet Source Data | Der Prozess muss die Quelldaten für die Berechnungen aus existierenden Dateien im Pfad `/data/parquet/<ticker>/<timeframe>.parquet` laden. | - |
| F-OUT-080 | Storage | Parquet Target Overwrite | Die berechneten Features müssen vorerst per Overwrite-Verfahren unter dem Pfad `/data/parquet/<ticker>/<timeframe>_features.parquet` abgespeichert werden. | - |
| F-LOG-090 | Error | Terminal Error Logging | Schlägt die Berechnung für einen Ticker fehl, muss der Fehler ins Terminal geloggt werden, während der Prozess die übrigen Ticker weiter abarbeitet. | - |
| F-PRC-100 | Data | Backfill Missing Data | Wenn historische Daten für Indikatoren fehlen, muss das System künstliche Daten durch Rückwärts-Duplikation des ersten Tages erzeugen, sodass Indikatoren niemals Null/0 sind. | - |
| F-CFG-010 | Config | Aggregation Tagging | In der `features.json` können Indikatoren mit dem Parameter `"aggregation": "all"` markiert werden, um sie als aktienübergreifende Metriken zu kennzeichnen. | - |
| F-PRC-110 | Process | Globale Vorab-Berechnung | Markierte "aggregated"-Indikatoren werden in der Pre-Compute-Phase (Pass 0) aufgesammelt und global exakt einmal über den gesamten Marktdatensatz berechnet. | - |
| F-PRC-120 | Process | Globale Injektion | Die resultierende Zeitreihe der aggregierten Metriken wird in Pass 1 automatisiert als Spalte in die Parquet-Strukturen aller individuellen Ticker injiziert. | - |
