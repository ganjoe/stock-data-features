import pandas as pd
import numpy as np
from typing import List
from config_parser import FeatureConfig, FeatureType

class TechnicalCalculator:
    def calculate_features(self, df: pd.DataFrame, configs: List[FeatureConfig]) -> pd.DataFrame:
        """Applies technical indicators to the dataframe based on provided configs."""
        if df.empty:
            return df
            
        # Work on a copy to avoid SettingWithCopyWarning
        df = df.copy()
        
        for config in configs:
            if config.feature_type == FeatureType.SMA:
                df = self._calc_sma(df, config)
            elif config.feature_type == FeatureType.EMA:
                df = self._calc_ema(df, config)
            elif config.feature_type == FeatureType.BOLLINGER_BAND:
                df = self._calc_bb(df, config)
            elif config.feature_type == FeatureType.STOCHASTIC:
                df = self._calc_stoch(df, config)
            elif config.feature_type == FeatureType.IBD_RS:
                df = self._calc_ibd_rs_raw(df, config)
            else:
                # F-PRC-047: Other features are stubs
                df = self._calc_stubs(df, config)
        
        # Minervini Trend Score muss nach IBD_RS berechnet werden, da es das RS-Rating verwendet
        minervini_configs = [c for c in configs if c.feature_type == FeatureType.MINERVINI_TREND]
        for config in minervini_configs:
            df = self._calc_minervini_trend(df, config)
                
        return df
    
    def _get_ma_series(self, df: pd.DataFrame, column: str, window: int, ma_type: FeatureType) -> pd.Series:
        """Shared helper to calculate MA with prepending to handle early data."""
        if df.empty or window <= 0:
            return pd.Series(dtype=float)
            
        if window == 1:
            return df[column].reset_index(drop=True).bfill().fillna(0)
            
        # F-PRC-100: Prepend duplicated first row to handle early data / IPO
        first_row = df.iloc[[0]]
        prepended = pd.concat([first_row] * (window - 1), ignore_index=True)
        calc_df = pd.concat([prepended, df], ignore_index=True)
        
        if ma_type == FeatureType.EMA:
            ma_series = calc_df[column].ewm(span=window, adjust=False).mean()
        else:
            ma_series = calc_df[column].rolling(window=window).mean()
            
        # Slice back to original size and bfill/fillna(0) for safety
        result = ma_series.iloc[window-1:].reset_index(drop=True)
        return result.bfill().fillna(0)

    def _calc_sma(self, df: pd.DataFrame, config: FeatureConfig) -> pd.DataFrame:
        window = config.window or 10
        df[config.feature_id] = self._get_ma_series(df, 'close', window, FeatureType.SMA).values
        return df

    def _calc_ema(self, df: pd.DataFrame, config: FeatureConfig) -> pd.DataFrame:
        window = config.window or 10
        df[config.feature_id] = self._get_ma_series(df, 'close', window, FeatureType.EMA).values
        return df

    def _calc_bb(self, df: pd.DataFrame, config: FeatureConfig) -> pd.DataFrame:
        """Calculates Bollinger Bands (upper, lower, average, bandwidth)."""
        window = config.window or 20
        ma_type_str = config.additional_params.get("type", "SMA").upper()
        ma_type = FeatureType.EMA if ma_type_str == "EMA" else FeatureType.SMA
        
        # Average line (SMA or EMA)
        avg_line = self._get_ma_series(df, 'close', window, ma_type)
        
        # Prepend for std dev calculation consistency
        first_row = df.iloc[[0]]
        prepended = pd.concat([first_row] * (window - 1), ignore_index=True)
        calc_df = pd.concat([prepended, df], ignore_index=True)
        std_dev = calc_df['close'].rolling(window=window).std().iloc[window-1:].reset_index(drop=True).bfill().fillna(0)
        
        df[f"{config.feature_id}_avg"] = avg_line.values
        df[f"{config.feature_id}_upper"] = (avg_line + (std_dev * 2)).values
        df[f"{config.feature_id}_lower"] = (avg_line - (std_dev * 2)).values
        
        # Bandwidth: (Upper - Lower) / Average
        # F-FEA-040: Protection against division by zero (P=0)
        avg_vals = df[f"{config.feature_id}_avg"].values
        safe_avg = np.where(avg_vals == 0, np.nan, avg_vals)
        
        df[f"{config.feature_id}_bandwidth"] = ((df[f"{config.feature_id}_upper"].values - df[f"{config.feature_id}_lower"].values) / safe_avg)
        df[f"{config.feature_id}_bandwidth"] = df[f"{config.feature_id}_bandwidth"].fillna(0)
        
        return df

    def _calc_stoch(self, df: pd.DataFrame, config: FeatureConfig) -> pd.DataFrame:
        """Calculates Stochastic %K and %D."""
        window = config.window or 14
        smooth_conf = config.additional_params.get("d", {})
        smooth_window = smooth_conf.get("window", 3)
        smooth_type_str = smooth_conf.get("type", "SMA").upper()
        smooth_type = FeatureType.EMA if smooth_type_str == "EMA" else FeatureType.SMA
        
        # %K = (Current Close - Lowest Low) / (Highest High - Lowest Low) * 100
        low_min = df['low'].rolling(window=window, min_periods=1).min()
        high_max = df['high'].rolling(window=window, min_periods=1).max()
        
        k_series = 100 * (df['close'] - low_min) / (high_max - low_min)
        k_series = k_series.fillna(0)
        
        df[f"{config.feature_id}_k"] = k_series.values
        
        # %D = MA of %K
        k_df = pd.DataFrame({'val': k_series})
        df[f"{config.feature_id}_d"] = self._get_ma_series(k_df, 'val', smooth_window, smooth_type).values
        
        return df
        
    def _calc_stubs(self, df: pd.DataFrame, config: FeatureConfig) -> pd.DataFrame:
        return df

    def _calc_ibd_rs_raw(self, df: pd.DataFrame, config: FeatureConfig) -> pd.DataFrame:
        """Calculates the raw ROC score for IBD RS rating."""
        if len(df) < 1:
            df[f"{config.feature_id}_raw"] = 0.0
            return df
            
        # P_Heute = Aktie.Schlusskurs[Heute]
        # P_N = Aktie.Schlusskurs[Vor_N_Tagen]
        # ROC_N = ((P_Heute - P_N) / P_N) * 100
        # pandas pct_change(periods=N) * 100 computes exactly this.
        
        roc_63 = df['close'].pct_change(periods=63) * 100
        roc_126 = df['close'].pct_change(periods=126) * 100
        roc_189 = df['close'].pct_change(periods=189) * 100
        roc_252 = df['close'].pct_change(periods=252) * 100
        
        # Use fillna(0) and replace inf (from zero prices) with 0.0
        # F-FEA-050: Robustness against P=0
        roc_63 = roc_63.replace([np.inf, -np.inf], np.nan).fillna(0)
        roc_126 = roc_126.fillna(0).replace([np.inf, -np.inf], 0)
        roc_189 = roc_189.fillna(0).replace([np.inf, -np.inf], 0)
        roc_252 = roc_252.fillna(0).replace([np.inf, -np.inf], 0)
        
        raw_score = (2 * roc_63) + roc_126 + roc_189 + roc_252
        
        # We append '_raw' because processor.py will pull this out and compute the cross-sectional rank
        df[f"{config.feature_id}_raw"] = raw_score.values
        return df

    def _calc_minervini_trend(self, df: pd.DataFrame, config: FeatureConfig) -> pd.DataFrame:
        """
        Berechnet den Minervini Trend Template Score basierend auf 8 Kriterien.
        
        Die 8 Bedingungen:
        1. Preis liegt über SMA_150 und SMA_200
        2. SMA_150 liegt über SMA_200
        3. SMA_200 tendiert seit mindestens einem Monat aufwärts (> vor 20 Tagen)
        4. SMA_50 liegt über SMA_150 und SMA_200
        5. Preis liegt über SMA_50
        6. Preis ist mindestens 30% höher als das 52-Wochen-Tief (260 Tage)
        7. Preis ist nicht weiter als 25% vom 52-Wochen-Hoch entfernt
        8. RS_Rating >= 70
        
        Verwendet das bereits berechnete ibd_rs_raw Rating für Bedingung 8.
        
        Returns: Score (0-8), Prozent_Score (0-100%), is_trend_template (bool)
        """
        if len(df) < 260:
            # Nicht genügend Daten für vollständige Berechnung
            df["minervini_score"] = pd.Series(0, index=df.index)
            df["minervini_percent"] = pd.Series(0.0, index=df.index)
            df["minervini_trend_template"] = pd.Series(False, index=df.index)
            return df
        
        # Gleitende Durchschnitte berechnen
        sma_50 = self._get_ma_series(df, 'close', 50, FeatureType.SMA).values
        sma_150 = self._get_ma_series(df, 'close', 150, FeatureType.SMA).values
        sma_200 = self._get_ma_series(df, 'close', 200, FeatureType.SMA).values
        
        # SMA_200 vor 20 Tagen (für Bedingung 3)
        sma_200_vor_20 = np.roll(sma_200, 20)
        sma_200_vor_20[:20] = sma_200[:1]  # Füllen mit erstem Wert für frühe Daten
        
        # Extreme der letzten 52 Wochen (260 Tage)
        # Prepend für korrekte Berechnung am Anfang
        first_row = df.iloc[[0]]
        prepended = pd.concat([first_row] * 259, ignore_index=True)
        calc_df_52w = pd.concat([prepended, df], ignore_index=True)
        
        high_52w = calc_df_52w['high'].rolling(window=260).max().iloc[259:].reset_index(drop=True).values
        low_52w = calc_df_52w['low'].rolling(window=260).min().iloc[259:].reset_index(drop=True).values
        
        # IBD RS Rating verwenden (bereits als ibd_rs_raw Spalte vorhanden)
        # Falls noch nicht berechnet, berechne es hier als Fallback
        if 'ibd_rs_raw' in df.columns:
            rs_rating = df['ibd_rs_raw'].values
        else:
            # Fallback-Berechnung von RS Rating (IBD Methode)
            roc_63 = df['close'].pct_change(periods=63) * 100
            roc_126 = df['close'].pct_change(periods=126) * 100
            roc_189 = df['close'].pct_change(periods=189) * 100
            roc_252 = df['close'].pct_change(periods=252) * 100
            
            # Robustness against inf values
            roc_63 = roc_63.replace([np.inf, -np.inf], np.nan).fillna(0)
            roc_126 = roc_126.replace([np.inf, -np.inf], np.nan).fillna(0)
            roc_189 = roc_189.replace([np.inf, -np.inf], np.nan).fillna(0)
            roc_252 = roc_252.replace([np.inf, -np.inf], np.nan).fillna(0)
            
            raw_score = (2 * roc_63) + roc_126 + roc_189 + roc_252
            rs_rating = raw_score.values
        
        # Cross-sectional Ranking für RS Rating anwenden (über alle Ticker)
        # Dies ist eine vereinfachte Berechnung - das eigentliche cross-sectional Ranking
        # erfolgt im processor.py über alle Ticker hinweg. Hier verwenden wir den Rohwert
        # als Platzhalter, da Minervini typischerweise zusammen mit IBD_RS berechnet wird.
        N = len(rs_rating)
        if N > 1:
            rank_df = pd.DataFrame({'rs': rs_rating}).rank(axis=0, na_option='bottom')
            rs_rating_ranked = ((rank_df['rs'] - 1) / (N - 1) * 98 + 1).round().clip(1, 99).astype(float).values
        else:
            rs_rating_ranked = np.full(N, 50.0)
        
        aktueller_preis = df['close'].values
        
        # Initialisiere Score-Arrays
        score = np.zeros(len(df), dtype=int)
        
        # Bedingung 1: Preis > SMA_150 UND Preis > SMA_200
        cond1 = (aktueller_preis > sma_150) & (aktueller_preis > sma_200)
        score += cond1.astype(int)
        
        # Bedingung 2: SMA_150 > SMA_200
        cond2 = sma_150 > sma_200
        score += cond2.astype(int)
        
        # Bedingung 3: SMA_200 > SMA_200_vor_20_Tagen (aufwärts tendierend)
        cond3 = sma_200 > sma_200_vor_20
        score += cond3.astype(int)
        
        # Bedingung 4: SMA_50 > SMA_150 UND SMA_50 > SMA_200
        cond4 = (sma_50 > sma_150) & (sma_50 > sma_200)
        score += cond4.astype(int)
        
        # Bedingung 5: Preis > SMA_50
        cond5 = aktueller_preis > sma_50
        score += cond5.astype(int)
        
        # Bedingung 6: Preis >= Tief_52Wochen * 1.30 (mindestens 30% höher als 52-Wochen-Tief)
        cond6 = aktueller_preis >= (low_52w * 1.30)
        score += cond6.astype(int)
        
        # Bedingung 7: Preis >= Hoch_52Wochen * 0.75 (nicht weiter als 25% vom Hoch entfernt)
        cond7 = aktueller_preis >= (high_52w * 0.75)
        score += cond7.astype(int)
        
        # Bedingung 8: RS_Rating >= 70
        cond8 = rs_rating_ranked >= 70
        score += cond8.astype(int)
        
        # Trend Template erfüllt wenn Score == 8
        is_trend_template = score == 8
        
        df["minervini_score"] = pd.Series(score, index=df.index)
        df["minervini_trend_template"] = pd.Series(is_trend_template, index=df.index)
        
        return df
