import os
import logging
from pathlib import Path
from typing import List, Optional, Any
import pandas as pd
from parquet_io import ParquetStorage

logger = logging.getLogger(__name__)

class ScannerEngine:
    def __init__(self, storage: ParquetStorage, watchlists_dir: str):
        self.storage = storage
        self.watchlists_dir = Path(watchlists_dir)
        self.watchlists_dir.mkdir(parents=True, exist_ok=True)

    def run_scan(
        self, 
        scanner_name: str, 
        condition: str, 
        value: Optional[float] = None, 
        min_val: Optional[float] = None, 
        max_val: Optional[float] = None
    ) -> List[str]:
        """
        Runs a universal scan across all available tickers.
        
        Args:
            scanner_name: The name of the feature column to scan (e.g., 'ibd_rs').
            condition: The filter condition ('higher', 'lower', 'range').
            value: Threshold for 'higher' or 'lower'.
            min_val: Minimum for 'range'.
            max_val: Maximum for 'range'.
            
        Returns:
            List of tickers that passed the filter.
        """
        tickers = self.storage.get_available_tickers()
        matched_tickers = []
        
        logger.info(f"▶️  Starting universal scan: {scanner_name} {condition} {value or (min_val, max_val)}")
        
        for ticker in tickers:
            try:
                # Load the features file for the ticker. Timeframe defaults to 1D features.
                df = self.storage.load_ticker_data(ticker, "1D_features")
                
                if df.empty or scanner_name not in df.columns:
                    continue
                
                # Get the latest non-NaN value for the requested indicator
                series = df[scanner_name].dropna()
                if series.empty:
                    continue
                latest_value = series.iloc[-1]
                
                if pd.isna(latest_value):
                    continue
                
                # Evaluate condition
                is_match = False
                if condition == "higher" and value is not None:
                    is_match = float(latest_value) >= value
                elif condition == "lower" and value is not None:
                    is_match = float(latest_value) <= value
                elif condition == "range" and min_val is not None and max_val is not None:
                    is_match = min_val <= float(latest_value) <= max_val
                
                if is_match:
                    matched_tickers.append(ticker)
                    
            except FileNotFoundError:
                # No features calculated yet for this ticker, skip gracefully
                continue
            except Exception as e:
                logger.warning(f"⚠️  Error scanning {ticker}: {e}")
                continue
        
        logger.info(f"✅ Scan finished: {len(matched_tickers)} matches found.")
        
        # Write results to watchlist file (atomic write)
        self._write_watchlist(scanner_name, matched_tickers)
        
        return matched_tickers

    def _write_watchlist(self, scanner_name: str, tickers: List[str]) -> None:
        """Writes the ticker list to data/watchlists/<scanner_name>.txt (atomic)."""
        output_path = self.watchlists_dir / f"{scanner_name}.txt"
        tmp_path = output_path.with_suffix(".txt.tmp")
        
        try:
            with open(tmp_path, "w") as f:
                if tickers:
                    f.write("\n".join(sorted(tickers)) + "\n")
            
            os.replace(tmp_path, output_path)
            logger.info(f"📁 Updated watchlist: {output_path}")
        except Exception as e:
            logger.error(f"❌ Failed to write watchlist {scanner_name}: {e}")
            if tmp_path.exists():
                tmp_path.unlink()
            raise
