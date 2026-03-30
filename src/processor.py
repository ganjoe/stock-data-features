import logging
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List, Optional, Dict
import pandas as pd
from dataclasses import dataclass

from config_parser import FeatureConfig, ProcessingContext
from calculator import TechnicalCalculator
from parquet_io import ParquetStorage

# Setup logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

@dataclass
class TickerProcessResult:
    """Result of processing a single ticker."""
    ticker: str
    timeframe: str
    success: bool
    data_points: int = 0
    error_message: Optional[str] = None
    cross_sectional_data: Optional[Dict[str, pd.Series]] = None

class FeatureProcessor:
    def __init__(self, context: ProcessingContext, storage: ParquetStorage, calculator: TechnicalCalculator):
        self.context = context
        self.storage = storage
        self.calculator = calculator

    def process_all_tickers(self, tickers: List[str]) -> List[TickerProcessResult]:
        """Spawns parallel processes to compute features for all tickers."""
        import time
        start_time = time.perf_counter()
        results = []
        total_tickers = len(tickers)
        completed = 0
        last_logged_pct = 0
        
        # Parallel execution on Ticker level (ThreadPool: no pickle overhead,
        # pandas/numpy C-extensions release the GIL for true parallelism)
        with ThreadPoolExecutor(max_workers=self.context.thread_count) as executor:
            future_to_ticker = {executor.submit(self._process_single_ticker, ticker): ticker for ticker in tickers}
            
            for future in as_completed(future_to_ticker):
                ticker = future_to_ticker[future]
                completed += 1
                try:
                    ticker_results = future.result()
                    results.extend(ticker_results)
                except Exception as exc:
                    logger.error(f"Ticker {ticker} generated an exception: {exc}")
                    logger.error(traceback.format_exc())
                    # Add error result for overall tracking
                    results.append(TickerProcessResult(ticker, "all", False, 0, str(exc)))
                
                # Progress logging (roughly every 10%)
                pct = int((completed / total_tickers) * 100)
                if pct - last_logged_pct >= 10 or completed == total_tickers:
                    logger.info(f"⚙️  Feature processing: {pct}% ({completed}/{total_tickers} tickers)")
                    last_logged_pct = pct

        elapsed = time.perf_counter() - start_time
        successful_results = [r for r in results if r.success]
        failed_results = [r for r in results if not r.success]
        
        total_pts = sum(r.data_points for r in successful_results)
        num_features = len(self.context.features) if hasattr(self.context, 'features') and self.context.features else 0
        
        logger.info("═══════════════════════════════════════════════════════════════")
        logger.info(f"✨ Feature Calculation Summary:")
        logger.info(f"   • Tickers processed : {total_tickers}")
        logger.info(f"   • Data points       : {total_pts:,d}".replace(",", "."))
        logger.info(f"   • Features per point: {num_features}")
        logger.info(f"   • Duration          : {elapsed:.2f}s")
        if failed_results:
            logger.warning(f"   • Failed tickers    : {len(failed_results)}")
        logger.info("═══════════════════════════════════════════════════════════════")
                    
        # --- PASS 2 & 3: Cross-Sectional Ranking ---
        self._process_cross_sectional_features(results)
        
        return results

    def _process_cross_sectional_features(self, results: List[TickerProcessResult]) -> None:
        """
        Gathers all raw cross-sectional data extracted from the parallel phase,
        computes percentiles centrally, and then injects them back into the parquet files.
        """
        # Dictionary structure: {timeframe: {feature_id_raw: {ticker: pd.Series}}}
        cs_data_by_tf: Dict[str, Dict[str, Dict[str, pd.Series]]] = {}
        
        for r in results:
            if r.success and r.cross_sectional_data:
                tf = r.timeframe
                if tf not in cs_data_by_tf:
                    cs_data_by_tf[tf] = {}
                
                for col_name, series in r.cross_sectional_data.items():
                    if col_name not in cs_data_by_tf[tf]:
                        cs_data_by_tf[tf][col_name] = {}
                    cs_data_by_tf[tf][col_name][r.ticker] = series
        
        for tf, features in cs_data_by_tf.items():
            for feature_raw_col, ticker_series_dict in features.items():
                # E.g., feature_raw_col = 'ibd_rs_raw', target_col = 'ibd_rs'
                target_col = feature_raw_col.replace("_raw", "")
                
                # Build an aligned DataFrame where index is datetime/date, columns are TICKERS
                central_df = pd.DataFrame(ticker_series_dict)
                
                # Cross-sectional ranking per row (date)
                # Formula for percentile: (rank - 1) / (N - 1) * 98 + 1
                # Where rank is 1 to N, and N is the number of tickers WITH DATA for that date.
                # NaN values are excluded from ranking (na_option='keep').
                
                # Dynamic N per row: only count non-NaN values
                N_per_row = central_df.notna().sum(axis=1)
                
                # Rank only non-NaN values; NaN stays NaN
                rank_df = central_df.rank(axis=1, na_option='keep')
                
                # Build rating: (rank - 1) / (N - 1) * 98 + 1, clipped to [1, 99]
                # For rows where N <= 1, assign 50
                N_minus_1 = (N_per_row - 1).clip(lower=1)
                rating_df = ((rank_df.sub(1)).div(N_minus_1, axis=0) * 98 + 1)
                rating_df = rating_df.round().clip(1, 99)
                
                # Where N <= 1, override to 50
                single_ticker_rows = N_per_row <= 1
                if single_ticker_rows.any():
                    rating_df.loc[single_ticker_rows] = 50
                
                rating_df = rating_df.astype("Int64")
                
                logger.info(f"⚙️  Injecting cross-sectional feature '{target_col}' into {len(central_df.columns)} tickers for timeframe {tf}...")
                
                # Parallel inject pass using ThreadPoolExecutor
                def _inject_rating(ticker_name: str) -> None:
                    try:
                        df = self.storage.load_ticker_data(ticker_name, f"{tf}_features")
                        ticker_ratings = rating_df[ticker_name].dropna()
                        df[target_col] = df['timestamp'].map(ticker_ratings).astype("Int64")
                        self.storage.save_ticker_features(ticker_name, tf, df)
                    except Exception as e:
                        logger.error(f"Failed to inject {target_col} for {ticker_name}: {e}")

                with ThreadPoolExecutor(max_workers=self.context.thread_count) as inject_executor:
                    futures = [inject_executor.submit(_inject_rating, t) for t in central_df.columns]
                    for f in as_completed(futures):
                        try:
                            f.result()
                        except Exception:
                            pass  # already logged inside _inject_rating

    def _process_single_ticker(self, ticker: str) -> List[TickerProcessResult]:
        """The atomic unit of work executed by worker threads/processes."""
        ticker_results = []
        
        for tf in self.context.timeframes:
            try:
                # 1. Load Data
                df = self.storage.load_ticker_data(ticker, tf)
                
                # 2. Calculate Features
                df_with_features = self.calculator.calculate_features(df, self.context.features)
                
                # Extract cross-sectional data (columns ending with '_raw') BEFORE saving
                cs_data = {}
                raw_cols = [c for c in df_with_features.columns if c.endswith("_raw")]
                for col in raw_cols:
                    # Index the series by timestamp so cross-sectional alignment works properly across all tickers
                    series = df_with_features[col].copy()
                    if 'timestamp' in df_with_features.columns:
                        series.index = df_with_features['timestamp']
                    cs_data[col] = series
                    df_with_features.drop(columns=[col], inplace=True)
                
                # 3. Save Data (without raw columns)
                self.storage.save_ticker_features(ticker, tf, df_with_features)
                
                pts = len(df_with_features) if df_with_features is not None else 0
                ticker_results.append(TickerProcessResult(ticker, tf, True, data_points=pts, cross_sectional_data=cs_data))
                # Removed per-ticker logging to reduce noise
                
            except Exception as e:
                error_msg = f"Error processing {ticker} [{tf}]: {str(e)}"
                logger.error(error_msg)
                ticker_results.append(TickerProcessResult(ticker, tf, False, 0, error_msg))
                
        return ticker_results
