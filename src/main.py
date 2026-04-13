"""
main.py — Stock Data Features Service
Standalone FastAPI microservice for calculating technical indicators.
Triggered via POST /features/calculate from stock-data-node or manually.
"""
from __future__ import annotations

import json
import logging
import os
import re
import sys
from pathlib import Path

from typing import Optional

import numpy as np
import pandas as pd

import uvicorn
from fastapi import FastAPI, status
from fastapi.responses import JSONResponse, StreamingResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, field_validator

# ─── Bootstrap: ensure src/ is on the path ──────────────────────
sys.path.insert(0, str(Path(__file__).parent))

from calculator import TechnicalCalculator
from parquet_io import ParquetStorage
from processor import FeatureProcessor
from job_manager import JobManager
from scanner import ScannerEngine


# ─── Request Models ──────────────────────────────────────────────

class MARequest(BaseModel):
    """Request body for on-the-fly moving average calculation."""
    ticker: str                          # e.g. "AAPL"
    ma_type: str                         # "sma" or "ema"
    chart_timeframe: str = "1D"          # source data timeframe, e.g. "1D"
    ma_window: int                       # MA period, e.g. 50

    @field_validator("ma_type")
    @classmethod
    def validate_ma_type(cls, v: str) -> str:
        v = v.upper()
        if v not in ("SMA", "EMA"):
            raise ValueError(f"ma_type must be 'sma' or 'ema', got '{v}'")
        return v

    @field_validator("ma_window")
    @classmethod
    def validate_window(cls, v: int) -> int:
        if v < 1 or v > 500:
            raise ValueError(f"ma_window must be between 1 and 500, got {v}")
        return v


class RSRequest(BaseModel):
    """Request body for on-the-fly RS Rating calculation."""
    ticker: str                                  # e.g. "AAPL"
    benchmark: Optional[str] = None              # e.g. "SPX", None = vs all tickers
    chart_timeframe: str = "1D"                  # source data timeframe


class MinerviniRequest(BaseModel):
    """Request body for on-the-fly Minervini Trend Template calculation."""
    ticker: str                                  # e.g. "AAPL"
    chart_timeframe: str = "1D"                  # source data timeframe

class ScannerRequest(BaseModel):
    """Request body for universal indicator scanner."""
    scanner_name: str                            # e.g. "ibd_rs" (also the column name to scan)
    condition: str                               # "higher", "lower", "range"
    value: Optional[float] = None                # threshold for higher/lower
    min_val: Optional[float] = None              # min for range
    max_val: Optional[float] = None              # max for range

    @field_validator("condition")
    @classmethod
    def validate_condition(cls, v: str) -> str:
        v = v.lower()
        if v not in ("higher", "lower", "range"):
            raise ValueError(f"condition must be 'higher', 'lower', or 'range', got '{v}'")
        return v

# ─── Logging Setup ───────────────────────────────────────────────

GREY = "\033[90m"
CYAN = "\033[36m"
MAGENTA = "\033[35m"
YELLOW = "\033[33m"
RED = "\033[31m"
BOLD_RED = "\033[31;1m"
RESET = "\033[0m"

TICKER_RE = re.compile(r"\b([A-Z]{1,5})\b")
NUMBER_RE = re.compile(r"(\b\d+(\.\d+)?\b)")


class ColoredFormatter(logging.Formatter):
    COLORS = {
        logging.DEBUG: GREY,
        logging.INFO: RESET,
        logging.WARNING: YELLOW,
        logging.ERROR: RED,
        logging.CRITICAL: BOLD_RED,
    }

    def format(self, record: logging.LogRecord) -> str:
        color = self.COLORS.get(record.levelno, RESET)
        msg = str(record.msg)
        if record.args:
            try:
                msg = msg % record.args
            except Exception:
                pass
        msg = TICKER_RE.sub(f"{CYAN}\\1{RESET}{color}", msg)
        msg = NUMBER_RE.sub(f"{MAGENTA}\\1{RESET}{color}", msg)
        time_str = self.formatTime(record, "%H:%M:%S")
        level_str = record.levelname.ljust(8)
        module_str = record.name[:20].ljust(20)
        if "════" in msg:
            return f"{color}{msg}{RESET}"
        return f"{time_str} | {color}{level_str}{RESET} | {GREY}{module_str}{RESET} | {color}{msg}{RESET}"


def configure_logging(log_dir: str) -> None:
    log_dir_path = Path(log_dir)
    log_dir_path.mkdir(parents=True, exist_ok=True)

    fmt = "%(asctime)s | %(levelname)-8s | %(name)-25s | %(message)s"
    datefmt = "%d.%m.%Y %H:%M:%S"
    formatter = logging.Formatter(fmt, datefmt=datefmt)

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)

    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setLevel(logging.DEBUG)
    stream_handler.setFormatter(ColoredFormatter())
    root.addHandler(stream_handler)

    file_handler = logging.FileHandler(log_dir_path / "error.log", encoding="utf-8")
    file_handler.setLevel(logging.ERROR)
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)

    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)


# ─── Config ──────────────────────────────────────────────────────

logger = logging.getLogger(__name__)

BASE_DIR = Path(os.environ.get("APP_BASE_DIR", Path(__file__).parent.parent))
CONFIG_DIR = str(BASE_DIR / "config")
LOG_DIR = str(BASE_DIR / "logs")
DATA_DIR = str(BASE_DIR / "data" / "parquet")
WATCHLISTS_DIR = str(BASE_DIR / "data" / "watchlists")
API_PORT = int(os.environ.get("FEATURES_API_PORT", "8003"))


def _load_settings() -> dict:
    """Load settings.json for processing_threads etc."""
    settings_path = Path(CONFIG_DIR) / "settings.json"
    if settings_path.exists():
        with open(settings_path, "r") as f:
            return json.load(f)
    return {}


# ─── Feature Pipeline Runner ────────────────────────────────────

def run_feature_pipeline() -> None:
    """Runs the full feature calculation pipeline (blocking)."""
    config_parser = FeatureConfigParser(str(Path(CONFIG_DIR) / "features.json"))
    features = config_parser.parse()

    if not features:
        logger.info("ℹ️  No features defined in features.json. Skipping.")
        return

    settings = _load_settings()
    thread_count = settings.get("processing_threads", 4)

    ctx = ProcessingContext(
        thread_count=thread_count,
        data_dir=DATA_DIR,
        timeframes=["1D"],
        features=features,
    )

    storage = ParquetStorage(ctx.data_dir)
    raw_tickers = storage.get_available_tickers()

    if not raw_tickers:
        logger.info("ℹ️  No data available yet. Skipping feature calculation.")
        return

    # Pre-filter to exclude tickers missing the required parquet files
    # This avoids spawning processes just to catch FileNotFoundError
    valid_tickers = []
    base_dir = Path(ctx.data_dir)
    for t in raw_tickers:
        if all((base_dir / t / f"{tf}.parquet").exists() for tf in ctx.timeframes):
            valid_tickers.append(t)
            
    skipped = len(raw_tickers) - len(valid_tickers)
    if skipped > 0:
        logger.info("ℹ️  Skipped %d tickers without complete source files.", skipped)
        
    tickers = valid_tickers

    if not tickers:
        logger.info("ℹ️  No valid tickers with data found. Skipping.")
        return

    logger.info(
        "▶️  Calculating features for %d ticker(s) with %d thread(s)...",
        len(tickers),
        ctx.thread_count,
    )

    calculator = TechnicalCalculator()
    processor = FeatureProcessor(ctx, storage, calculator)
    results = processor.process_all_tickers(tickers)
    success_count = sum(1 for r in results if r.success)
    logger.info("✅ Feature calculation finished: %d/%d successful", success_count, len(results))


# ─── FastAPI App ─────────────────────────────────────────────────

def create_app() -> FastAPI:
    app = FastAPI(
        title="Stock Data Features API",
        description="Technical indicator calculation service.",
        version="1.0.0",
    )

    # ─── Static Files & UI ───────────────────────────────────────────
    # Mount the static directory for the minimalist dashboard
    static_path = Path(BASE_DIR) / "static"
    if not static_path.exists():
        static_path.mkdir(parents=True, exist_ok=True)
    app.mount("/static", StaticFiles(directory=str(static_path)), name="static")

    @app.get("/")
    async def root():
        """Redirect root to the minimalist dashboard."""
        return RedirectResponse(url="/static/index.html")

    job_manager = JobManager()
    storage = ParquetStorage(DATA_DIR)
    scanner_engine = ScannerEngine(storage, WATCHLISTS_DIR)

    @app.post("/scanner/run")
    async def run_universal_scanner(req: ScannerRequest):
        """
        Triggers a universal scan for a specific indicator.
        Filters tickers based on condition (higher/lower/range).
        Exports result to data/watchlists/<scanner_name>.txt.
        """
        try:
            matches = scanner_engine.run_scan(
                req.scanner_name, 
                req.condition, 
                req.value, 
                req.min_val, 
                req.max_val
            )
            return {
                "status": "success",
                "scanner": req.scanner_name,
                "matches_found": len(matches),
                "watchlist_path": f"{WATCHLISTS_DIR}/{req.scanner_name}.txt"
            }
        except Exception as e:
            return JSONResponse(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                content={"status": "error", "detail": str(e)}
            )

    @app.post("/features/calculate")
    async def trigger_feature_calculation(stream: bool = False):
        """
        Triggers the feature calculation process.
        Returns 202 if started, 409 if already running. (F-API-010, F-SYS-030)
        If stream=True, returns a StreamingResponse with real-time logs.
        """
        if stream:
            return StreamingResponse(
                job_manager.stream_feature_calculation(run_feature_pipeline),
                media_type="text/plain",
            )

        success = job_manager.start_feature_calculation(run_feature_pipeline)

        if success:
            return JSONResponse(
                status_code=status.HTTP_202_ACCEPTED,
                content={
                    "status": "Job started in background",
                    "hint": "Use ?stream=true to see real-time log output",
                },
            )
        else:
            return JSONResponse(
                status_code=status.HTTP_409_CONFLICT,
                content={
                    "status": "Ignored",
                    "detail": "A feature calculation process is already running.",
                },
            )

    @app.post("/features/ma")
    async def calculate_moving_average(req: MARequest):
        """
        On-the-fly moving average calculation for a single ticker.
        Loads OHLCV data, computes the requested MA, returns JSON arrays.
        """
        storage = ParquetStorage(DATA_DIR)
        calculator = TechnicalCalculator()

        # 1. Load source data
        try:
            df = storage.load_ticker_data(req.ticker, req.chart_timeframe)
        except FileNotFoundError:
            return JSONResponse(
                status_code=status.HTTP_404_NOT_FOUND,
                content={
                    "error": f"No data found for ticker '{req.ticker}' "
                             f"with timeframe '{req.chart_timeframe}'",
                },
            )

        if df.empty:
            return JSONResponse(
                status_code=status.HTTP_404_NOT_FOUND,
                content={"error": f"Data for '{req.ticker}' is empty"},
            )

        # 2. Calculate MA using existing calculator engine
        ma_type_enum = FeatureType.EMA if req.ma_type == "EMA" else FeatureType.SMA
        ma_series = calculator._get_ma_series(df, "close", req.ma_window, ma_type_enum)

        # 3. Build response — only timestamp, close, and the computed MA
        timestamps = df["timestamp"].tolist()
        closes = df["close"].tolist()
        ma_values = ma_series.tolist()

        ma_label = f"{req.ma_type.lower()}_{req.ma_window}"

        return {
            "ticker": req.ticker,
            "chart_timeframe": req.chart_timeframe,
            "ma_type": req.ma_type,
            "ma_window": req.ma_window,
            "ma_label": ma_label,
            "data_points": len(timestamps),
            "timestamps": timestamps,
            "close": closes,
            "values": ma_values,
        }

    @app.post("/features/rs")
    async def calculate_rs_rating(req: RSRequest):
        """
        On-the-fly RS Rating calculation.
        - Without benchmark: cross-sectional percentile rank (1-99) vs all tickers.
        - With benchmark:    relative strength of ticker vs benchmark ticker.
        Returns the most recent value.
        """

        storage = ParquetStorage(DATA_DIR)

        # 1. Load ticker data
        try:
            df_ticker = storage.load_ticker_data(req.ticker, req.chart_timeframe)
        except FileNotFoundError:
            return JSONResponse(
                status_code=status.HTTP_404_NOT_FOUND,
                content={"error": f"No data found for ticker '{req.ticker}'"},
            )

        if len(df_ticker) < 63:
            return JSONResponse(
                status_code=status.HTTP_400_BAD_REQUEST,
                content={"error": f"Not enough data for '{req.ticker}' (need >= 63 rows, have {len(df_ticker)})"},
            )

        # Periods and their IBD weights
        PERIODS_WEIGHTS = [(63, 2), (126, 1), (189, 1), (252, 1)]
        FULL_WEIGHT_SUM = 5  # 2+1+1+1

        def _compute_normalized_roc(close: "pd.Series") -> tuple:
            """
            IBD-style weighted ROC score with normalization.
            Returns (normalized_score, num_components, data_length).
            Only uses ROC components for which sufficient data exists.
            The score is scaled to a full 5-weight basis.
            """
            n = len(close)
            weighted_sum = 0.0
            used_weight = 0
            components_used = 0

            for period, weight in PERIODS_WEIGHTS:
                if n > period:
                    p_now = close.iloc[-1]
                    p_then = close.iloc[-period - 1]
                    if p_then != 0 and not np.isnan(p_then) and not np.isnan(p_now):
                        roc = ((p_now - p_then) / p_then) * 100
                        weighted_sum += roc * weight
                        used_weight += weight
                        components_used += 1

            if used_weight == 0:
                return 0.0, 0, n

            normalized = weighted_sum * (FULL_WEIGHT_SUM / used_weight)
            return round(normalized, 4), components_used, n

        # ── Mode A: vs specific benchmark ticker ──────────────────
        if req.benchmark is not None:
            try:
                df_bench = storage.load_ticker_data(req.benchmark, req.chart_timeframe)
            except FileNotFoundError:
                return JSONResponse(
                    status_code=status.HTTP_404_NOT_FOUND,
                    content={"error": f"No data found for benchmark '{req.benchmark}'"},
                )

            if len(df_bench) < 63:
                return JSONResponse(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    content={"error": f"Not enough data for benchmark '{req.benchmark}' (need >= 63, have {len(df_bench)})"},
                )

            ticker_raw, ticker_comp, _ = _compute_normalized_roc(df_ticker["close"])
            bench_raw, bench_comp, _ = _compute_normalized_roc(df_bench["close"])

            rs_relative = round(ticker_raw - bench_raw, 4)

            return {
                "ticker": req.ticker,
                "benchmark": req.benchmark,
                "mode": "vs_benchmark",
                "ticker_raw_score": ticker_raw,
                "ticker_components": ticker_comp,
                "benchmark_raw_score": bench_raw,
                "benchmark_components": bench_comp,
                "rs_relative": rs_relative,
                "interpretation": "positive = ticker outperforms benchmark",
            }

        # ── Mode B: vs all tickers (cross-sectional rank) ─────────
        all_tickers = storage.get_available_tickers()
        raw_scores: dict[str, float] = {}
        skipped = 0

        for t in all_tickers:
            try:
                df_t = storage.load_ticker_data(t, req.chart_timeframe)
                if len(df_t) >= 63:
                    score, comp, _ = _compute_normalized_roc(df_t["close"])
                    if comp > 0:
                        raw_scores[t] = score
                    else:
                        skipped += 1
                else:
                    skipped += 1
            except Exception:
                skipped += 1
                continue

        if req.ticker not in raw_scores:
            score, comp, _ = _compute_normalized_roc(df_ticker["close"])
            if comp > 0:
                raw_scores[req.ticker] = score

        N = len(raw_scores)
        if N <= 1:
            percentile = 50
        else:
            scores_series = pd.Series(raw_scores)
            ranks = scores_series.rank()
            percentile = int(round(((ranks[req.ticker] - 1) / (N - 1)) * 98 + 1))
            percentile = max(1, min(99, percentile))

        ticker_score = raw_scores.get(req.ticker, 0.0)

        return {
            "ticker": req.ticker,
            "benchmark": None,
            "mode": "vs_all",
            "rs_rating": percentile,
            "raw_score": ticker_score,
            "universe_size": N,
            "skipped_tickers": skipped,
            "interpretation": f"Outperforms {percentile}% of {N} tickers ({skipped} excluded due to insufficient data)",
        }

    @app.post("/features/minervini")
    async def calculate_minervini(req: MinerviniRequest):
        """
        On-the-fly Minervini Trend Template calculation.
        Computes all 8 conditions and returns the latest score.
        """
        storage = ParquetStorage(DATA_DIR)
        calculator = TechnicalCalculator()

        try:
            df = storage.load_ticker_data(req.ticker, req.chart_timeframe)
        except FileNotFoundError:
            return JSONResponse(
                status_code=status.HTTP_404_NOT_FOUND,
                content={"error": f"No data found for ticker '{req.ticker}'"},
            )

        if len(df) < 260:
            return JSONResponse(
                status_code=status.HTTP_400_BAD_REQUEST,
                content={
                    "error": f"Not enough data for '{req.ticker}' "
                             f"(need >= 260 rows for 52-week analysis, have {len(df)})",
                },
            )

        close = df["close"].values
        high = df["high"].values
        low = df["low"].values

        # --- Moving Averages ---
        sma_50 = calculator._get_ma_series(df, "close", 50, FeatureType.SMA).values
        sma_150 = calculator._get_ma_series(df, "close", 150, FeatureType.SMA).values
        sma_200 = calculator._get_ma_series(df, "close", 200, FeatureType.SMA).values

        # --- 52-Week High/Low ---
        high_52w = pd.Series(high).rolling(window=260, min_periods=1).max().values
        low_52w = pd.Series(low).rolling(window=260, min_periods=1).min().values

        # --- SMA 200 trend (vs 20 days ago) ---
        sma_200_20ago = np.roll(sma_200, 20)
        sma_200_20ago[:20] = sma_200[0]

        # --- RS Rating (try to load from pre-computed features) ---
        rs_rating = None
        rs_available = False
        try:
            df_feat = storage.load_ticker_data(req.ticker, f"{req.chart_timeframe}_features")
            if "ibd_rs" in df_feat.columns and len(df_feat) > 0:
                last_rs = df_feat["ibd_rs"].iloc[-1]
                if not pd.isna(last_rs):
                    rs_rating = int(last_rs)
                    rs_available = True
        except Exception:
            pass

        # --- Evaluate all 8 conditions (latest row) ---
        i = -1  # last row
        conditions = {
            "1_price_above_sma150_and_sma200": bool(close[i] > sma_150[i] and close[i] > sma_200[i]),
            "2_sma150_above_sma200": bool(sma_150[i] > sma_200[i]),
            "3_sma200_trending_up": bool(sma_200[i] > sma_200_20ago[i]),
            "4_sma50_above_sma150_and_sma200": bool(sma_50[i] > sma_150[i] and sma_50[i] > sma_200[i]),
            "5_price_above_sma50": bool(close[i] > sma_50[i]),
            "6_price_30pct_above_52w_low": bool(close[i] >= low_52w[i] * 1.30),
            "7_price_within_25pct_of_52w_high": bool(close[i] >= high_52w[i] * 0.75),
            "8_rs_rating_above_70": bool(rs_rating >= 70) if rs_available else None,
        }

        score = sum(1 for v in conditions.values() if v is True)
        max_score = 8 if rs_available else 7
        is_template = score == 8 and rs_available

        return {
            "ticker": req.ticker,
            "chart_timeframe": req.chart_timeframe,
            "score": score,
            "max_score": max_score,
            "is_trend_template": is_template,
            "conditions": conditions,
            "rs_rating": rs_rating,
            "rs_available": rs_available,
            "context": {
                "close": round(float(close[i]), 2),
                "sma_50": round(float(sma_50[i]), 2),
                "sma_150": round(float(sma_150[i]), 2),
                "sma_200": round(float(sma_200[i]), 2),
                "high_52w": round(float(high_52w[i]), 2),
                "low_52w": round(float(low_52w[i]), 2),
            },
        }

    @app.get("/status")
    async def get_status() -> dict:
        """Returns whether a feature calculation is currently running."""
        return {"is_running": job_manager.is_running}

    @app.get("/health")
    async def health_check() -> dict:
        """Simple liveness probe for Docker health checks."""
        return {"status": "ok"}

    return app


# ─── Entrypoint ──────────────────────────────────────────────────

def main() -> None:
    configure_logging(LOG_DIR)

    logger.info("═══════════════════════════════════════════════════════════════")
    logger.info("  Stock Data Features Service — starting up")
    logger.info("═══════════════════════════════════════════════════════════════")
    logger.info("ℹ️  Config dir: %s", CONFIG_DIR)
    logger.info("ℹ️  Data dir:   %s", DATA_DIR)
    logger.info("ℹ️  API port:   %d", API_PORT)

    app = create_app()

    uvicorn.run(
        app,
        host="0.0.0.0",
        port=API_PORT,
        log_level="warning",
    )


if __name__ == "__main__":
    main()
