from __future__ import annotations

import logging
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from config import AppConfig
from engine.base import BaseBacktestEngine
from schemas import BacktestJobDefinition, MarketDataCatalogEntry, StrategyBacktestSummary, StrategyDefinition

logger = logging.getLogger(__name__)

REQUIRED_PRICE_COLUMNS = {"datetime", "open", "high", "low", "close"}
EMPTY_TRADEBOOK_COLUMNS = [
    "ticker",
    "entry_time",
    "exit_time",
    "entry_price",
    "exit_price",
    "position",
    "quantity",
    "gross_pnl",
    "costs",
    "pnl",
    "return_pct",
    "exit_reason",
]
MARKET_DATA_CATEGORY_KEYWORDS: dict[str, set[str]] = {
    "crypto": {
        "crypto",
        "binance",
        "coinbase",
        "kraken",
        "bitcoin",
        "btc",
        "ethereum",
        "eth",
        "solana",
        "sol",
        "xrp",
        "doge",
        "ada",
        "bnb",
        "usdt",
        "usdc",
    },
    "indices": {
        "index",
        "indices",
        "nifty",
        "banknifty",
        "sensex",
        "sp500",
        "spx",
        "spy",
        "nasdaq",
        "ndx",
        "dow",
        "djia",
        "russell",
        "rut",
        "vix",
        "ftse",
        "dax",
        "nikkei",
        "hangseng",
        "stoxx",
    },
    "energy": {
        "energy",
        "crude",
        "oil",
        "wti",
        "brent",
        "natgas",
        "naturalgas",
        "gasoline",
        "heatingoil",
        "power",
    },
    "metals": {
        "metal",
        "metals",
        "gold",
        "silver",
        "copper",
        "platinum",
        "palladium",
        "xau",
        "xag",
    },
    "fx": {
        "forex",
        "fx",
        "eurusd",
        "usdjpy",
        "gbpusd",
        "usdchf",
        "audusd",
        "usdcad",
        "eur",
        "usd",
        "gbp",
        "jpy",
        "inr",
        "cad",
        "aud",
        "chf",
        "nzd",
    },
    "rates": {
        "bond",
        "bonds",
        "yield",
        "treasury",
        "gsec",
        "rates",
        "sofr",
    },
    "equities": {
        "equity",
        "equities",
        "stock",
        "stocks",
        "share",
        "shares",
        "nse",
        "bse",
        "nyse",
        "bhavcopy",
        "cash",
    },
    "commodities": {
        "commodity",
        "commodities",
        "agri",
        "wheat",
        "corn",
        "soy",
        "soybean",
        "sugar",
        "coffee",
        "cotton",
    },
}
MARKET_DATA_CATEGORY_PRIORITY = [
    "crypto",
    "indices",
    "energy",
    "metals",
    "fx",
    "rates",
    "equities",
    "commodities",
    "other",
]


@dataclass(slots=True)
class EngineSettings:
    stop_loss_pct: float
    take_profit_pct: float
    no_entry_after: str
    eod_exit_time: str
    brokerage_rate: float
    slippage_rate: float
    capital_per_trade: float


class BacktestEngine(BaseBacktestEngine):
    engine_name = "intraday_signal"

    def __init__(self, config: AppConfig) -> None:
        super().__init__(config)

    def load_market_data(self, job: BacktestJobDefinition | None = None) -> pd.DataFrame:
        candidate_paths = self._market_data_paths(job)
        if not candidate_paths:
            logger.warning("No market data files found. Tradebooks will be generated with headers only.")
            return self._empty_market_data()

        frames: list[pd.DataFrame] = []
        for path in candidate_paths:
            try:
                frame = self._read_market_data_file(path)
            except Exception as exc:
                logger.warning("Skipping unreadable market data file %s: %s", path, exc)
                continue
            if frame.empty:
                continue
            frames.append(frame)

        if not frames:
            logger.warning("Market data discovery succeeded, but no usable OHLCV rows were found.")
            return self._empty_market_data()

        data = pd.concat(frames, ignore_index=True)
        data["datetime"] = pd.to_datetime(data["datetime"], errors="coerce")
        data = data.dropna(subset=["datetime", "open", "high", "low", "close"])
        data = self._filter_market_data_for_job(data, job)
        data = data.sort_values(["ticker", "datetime"]).reset_index(drop=True)
        return data

    def scan_market_data_catalog(self, job: BacktestJobDefinition | None = None) -> list[MarketDataCatalogEntry]:
        entries = [self._build_market_data_catalog_entry(path) for path in self._market_data_paths(job)]
        if job is None or not job.market_categories:
            return entries
        allowed = {category.lower() for category in job.market_categories}
        return [entry for entry in entries if entry.inferred_category.lower() in allowed]

    @staticmethod
    def summarize_market_data_catalog(entries: list[MarketDataCatalogEntry]) -> dict[str, int]:
        counts = Counter(entry.inferred_category for entry in entries)
        return dict(sorted(counts.items(), key=lambda item: item[0]))

    def _settings_for_job(self, job: BacktestJobDefinition | None) -> EngineSettings:
        if job is None:
            return EngineSettings(
                stop_loss_pct=self.config.default_stop_loss_pct,
                take_profit_pct=self.config.default_take_profit_pct,
                no_entry_after=self.config.no_entry_after,
                eod_exit_time=self.config.eod_exit_time,
                brokerage_rate=self.config.brokerage_rate,
                slippage_rate=self.config.slippage_rate,
                capital_per_trade=self.config.capital_per_trade,
            )
        return EngineSettings(
            stop_loss_pct=job.stop_loss_pct,
            take_profit_pct=job.take_profit_pct,
            no_entry_after=job.no_entry_after,
            eod_exit_time=job.eod_exit_time,
            brokerage_rate=job.brokerage_rate,
            slippage_rate=job.slippage_rate,
            capital_per_trade=job.capital_per_trade,
        )

    @staticmethod
    def _filter_market_data_for_job(data: pd.DataFrame, job: BacktestJobDefinition | None) -> pd.DataFrame:
        if data.empty or job is None:
            return data

        filtered = data.copy()
        if job.market_categories:
            allowed = {category.lower() for category in job.market_categories}
            filtered = filtered[filtered["dataset_category"].astype(str).str.lower().isin(allowed)]

        if job.ticker_patterns:
            pattern = "|".join(f"(?:{item})" for item in job.ticker_patterns)
            filtered = filtered[filtered["ticker"].astype(str).str.contains(pattern, case=False, regex=True, na=False)]

        return filtered.reset_index(drop=True)

    def run_strategy(
        self,
        strategy: StrategyDefinition,
        market_data: pd.DataFrame,
        job: BacktestJobDefinition | None = None,
    ) -> tuple[pd.DataFrame, StrategyBacktestSummary]:
        if market_data.empty:
            return self._empty_tradebook(), StrategyBacktestSummary(
                strategy_name=strategy.strategy_name,
                engine_name=self.engine_name,
                job_id=job.job_id if job else "",
            )

        signal_frame = self._prepare_signals(market_data, strategy)
        tradebook = self._execute_backtest(signal_frame, self._settings_for_job(job))
        summary = self._build_summary(strategy.strategy_name, tradebook, market_data, job, self.engine_name)
        return tradebook, summary

    def _market_data_paths(self, job: BacktestJobDefinition | None = None) -> list[Path]:
        configured_files = job.market_data_files if job and job.market_data_files else self.config.market_data_files
        configured_dirs = job.market_data_dirs if job and job.market_data_dirs else self.config.market_data_dirs
        discovered: list[Path] = []
        for path in configured_files:
            if path.exists() and path.is_file():
                discovered.append(path)
        for directory in configured_dirs:
            if not directory.exists() or not directory.is_dir():
                continue
            for pattern in ("*.csv", "*.parquet"):
                discovered.extend(sorted(directory.rglob(pattern)))

        deduped: list[Path] = []
        seen: set[str] = set()
        for path in discovered:
            normalized = str(path.resolve())
            if normalized in seen:
                continue
            deduped.append(path)
            seen.add(normalized)
        return deduped

    def _read_market_data_file(self, path: Path) -> pd.DataFrame:
        if path.suffix.lower() == ".parquet":
            frame = pd.read_parquet(path)
        else:
            frame = pd.read_csv(path)
        return self._normalize_market_data_frame(frame, path.stem, path)

    def _build_market_data_catalog_entry(self, path: Path) -> MarketDataCatalogEntry:
        category, matched_terms = self._infer_market_data_category(path)
        return MarketDataCatalogEntry(
            path=str(path),
            filename=path.name,
            file_format=path.suffix.lower().lstrip("."),
            inferred_category=category,
            matched_terms=matched_terms,
        )

    def _infer_market_data_category(self, path: Path) -> tuple[str, list[str]]:
        tokens, normalized_text = self._dataset_name_features(path)
        best_category = "other"
        best_matches: list[str] = []

        for category in MARKET_DATA_CATEGORY_PRIORITY:
            if category == "other":
                continue
            keywords = MARKET_DATA_CATEGORY_KEYWORDS[category]
            matches = sorted(
                {
                    keyword
                    for keyword in keywords
                    if keyword in tokens or keyword in normalized_text
                }
            )
            if len(matches) > len(best_matches):
                best_category = category
                best_matches = matches

        return best_category, best_matches

    @staticmethod
    def _dataset_name_features(path: Path) -> tuple[set[str], str]:
        parts = [path.stem]
        for depth, parent in enumerate(path.parents):
            if depth >= 2:
                break
            if parent.name:
                parts.append(parent.name)
        text = " ".join(parts).lower()
        tokens = set(re.findall(r"[a-z0-9]+", text))
        normalized_text = re.sub(r"[^a-z0-9]+", "", text)
        return tokens, normalized_text

    def _normalize_market_data_frame(self, frame: pd.DataFrame, fallback_ticker: str, source_path: Path) -> pd.DataFrame:
        if frame.empty:
            return self._empty_market_data()

        local = frame.copy()
        local.columns = [str(column).strip().lower() for column in local.columns]

        rename_map = {}
        synonyms = {
            "datetime": ["datetime", "timestamp", "date_time", "time_stamp"],
            "open": ["open", "o"],
            "high": ["high", "h"],
            "low": ["low", "l"],
            "close": ["close", "c", "ltp"],
            "volume": ["volume", "vol"],
            "ticker": ["ticker", "symbol", "instrument", "tradingsymbol"],
        }
        for canonical_name, options in synonyms.items():
            for option in options:
                if option in local.columns:
                    rename_map[option] = canonical_name
                    break

        if "datetime" not in rename_map and {"date", "time"} <= set(local.columns):
            local["datetime"] = pd.to_datetime(local["date"].astype(str) + " " + local["time"].astype(str), errors="coerce")
        else:
            local = local.rename(columns=rename_map)

        if "datetime" not in local.columns and "date" in local.columns:
            local["datetime"] = pd.to_datetime(local["date"], errors="coerce")

        if "ticker" not in local.columns:
            local["ticker"] = fallback_ticker.upper()
        if "volume" not in local.columns:
            local["volume"] = 0.0

        missing = REQUIRED_PRICE_COLUMNS.difference(local.columns)
        if missing:
            raise ValueError(f"Missing OHLC columns: {sorted(missing)}")

        for column in ["open", "high", "low", "close", "volume"]:
            local[column] = pd.to_numeric(local[column], errors="coerce")

        category, _ = self._infer_market_data_category(source_path)
        local["source_path"] = str(source_path)
        local["dataset_category"] = category
        return local[
            ["datetime", "ticker", "open", "high", "low", "close", "volume", "source_path", "dataset_category"]
        ].copy()

    def _prepare_signals(self, market_data: pd.DataFrame, strategy: StrategyDefinition) -> pd.DataFrame:
        enriched_frames: list[pd.DataFrame] = []
        for ticker, ticker_df in market_data.groupby("ticker", sort=False):
            frame = ticker_df.copy().sort_values("datetime").reset_index(drop=True)
            frame = self._compute_indicators(frame, strategy)
            frame = self._apply_signal_logic(frame, strategy)
            frame["ticker"] = ticker
            enriched_frames.append(frame)
        return pd.concat(enriched_frames, ignore_index=True) if enriched_frames else self._empty_market_data()

    def _compute_indicators(self, frame: pd.DataFrame, strategy: StrategyDefinition) -> pd.DataFrame:
        local = frame.copy()
        close = local["close"]

        if "sma" in strategy.indicators:
            params = strategy.parameters.get("sma", {})
            fast = int(params.get("fast_window", 10))
            slow = int(params.get("slow_window", 20))
            local["sma_fast"] = close.rolling(fast).mean()
            local["sma_slow"] = close.rolling(slow).mean()

        if "ema" in strategy.indicators:
            params = strategy.parameters.get("ema", {})
            fast = int(params.get("fast_window", 12))
            slow = int(params.get("slow_window", 26))
            local["ema_fast"] = close.ewm(span=fast, adjust=False).mean()
            local["ema_slow"] = close.ewm(span=slow, adjust=False).mean()

        if "rsi" in strategy.indicators:
            params = strategy.parameters.get("rsi", {})
            period = int(params.get("period", 14))
            delta = close.diff()
            gain = delta.clip(lower=0).rolling(period).mean()
            loss = (-delta.clip(upper=0)).rolling(period).mean()
            rs = gain / loss.replace(0, np.nan)
            local["rsi"] = 100 - (100 / (1 + rs))

        if "macd" in strategy.indicators:
            params = strategy.parameters.get("macd", {})
            fast = int(params.get("fast_window", 12))
            slow = int(params.get("slow_window", 26))
            signal = int(params.get("signal_window", 9))
            ema_fast = close.ewm(span=fast, adjust=False).mean()
            ema_slow = close.ewm(span=slow, adjust=False).mean()
            local["macd_line"] = ema_fast - ema_slow
            local["macd_signal"] = local["macd_line"].ewm(span=signal, adjust=False).mean()
            local["macd_hist"] = local["macd_line"] - local["macd_signal"]

        if {"atr", "adx"} & set(strategy.indicators):
            period = int(
                strategy.parameters.get("atr", {}).get(
                    "period",
                    strategy.parameters.get("adx", {}).get("period", 14),
                )
            )
            tr = pd.concat(
                [
                    local["high"] - local["low"],
                    (local["high"] - close.shift(1)).abs(),
                    (local["low"] - close.shift(1)).abs(),
                ],
                axis=1,
            ).max(axis=1)
            local["atr"] = tr.rolling(period).mean()
            local["atr_pct"] = local["atr"] / close.replace(0, np.nan)
            local["atr_ma"] = local["atr_pct"].rolling(20).mean()

        if "vwap" in strategy.indicators:
            session_key = local["datetime"].dt.date
            typical_price = (local["high"] + local["low"] + local["close"]) / 3.0
            pv = typical_price * local["volume"].fillna(0)
            cumulative_volume = local["volume"].fillna(0).groupby(session_key).cumsum().replace(0, np.nan)
            local["vwap"] = pv.groupby(session_key).cumsum() / cumulative_volume

        if "bollinger_bands" in strategy.indicators:
            params = strategy.parameters.get("bollinger_bands", {})
            window = int(params.get("window", 20))
            num_std = float(params.get("num_std", 2))
            mid = close.rolling(window).mean()
            std = close.rolling(window).std(ddof=0)
            local["bb_mid"] = mid
            local["bb_upper"] = mid + (std * num_std)
            local["bb_lower"] = mid - (std * num_std)

        if "stochastic" in strategy.indicators:
            params = strategy.parameters.get("stochastic", {})
            k_period = int(params.get("k_period", 14))
            d_period = int(params.get("d_period", 3))
            rolling_low = local["low"].rolling(k_period).min()
            rolling_high = local["high"].rolling(k_period).max()
            local["stoch_k"] = 100 * (close - rolling_low) / (rolling_high - rolling_low).replace(0, np.nan)
            local["stoch_d"] = local["stoch_k"].rolling(d_period).mean()

        if "adx" in strategy.indicators:
            period = int(strategy.parameters.get("adx", {}).get("period", 14))
            up_move = local["high"].diff()
            down_move = -local["low"].diff()
            plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
            minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)
            atr = local["atr"].replace(0, np.nan)
            plus_di = 100 * pd.Series(plus_dm, index=local.index).rolling(period).sum() / atr
            minus_di = 100 * pd.Series(minus_dm, index=local.index).rolling(period).sum() / atr
            dx = ((plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)) * 100
            local["plus_di"] = plus_di
            local["minus_di"] = minus_di
            local["adx"] = dx.rolling(period).mean()

        if "momentum" in strategy.indicators:
            period = int(strategy.parameters.get("momentum", {}).get("period", 10))
            local["momentum"] = close - close.shift(period)

        if "roc" in strategy.indicators:
            period = int(strategy.parameters.get("roc", {}).get("period", 10))
            local["roc"] = close.pct_change(period) * 100

        if "volume_sma" in strategy.indicators:
            window = int(strategy.parameters.get("volume_sma", {}).get("window", 20))
            local["volume_sma"] = local["volume"].rolling(window).mean()

        if "obv" in strategy.indicators:
            direction = np.sign(close.diff()).fillna(0)
            local["obv"] = (direction * local["volume"].fillna(0)).cumsum()
            signal_window = int(strategy.parameters.get("obv", {}).get("signal_window", 10))
            local["obv_signal"] = local["obv"].ewm(span=signal_window, adjust=False).mean()

        if "cci" in strategy.indicators:
            period = int(strategy.parameters.get("cci", {}).get("period", 20))
            typical_price = (local["high"] + local["low"] + local["close"]) / 3.0
            sma = typical_price.rolling(period).mean()
            mean_dev = (typical_price - sma).abs().rolling(period).mean()
            local["cci"] = (typical_price - sma) / (0.015 * mean_dev.replace(0, np.nan))

        local["session_date"] = local["datetime"].dt.date
        return local

    def _apply_signal_logic(self, frame: pd.DataFrame, strategy: StrategyDefinition) -> pd.DataFrame:
        long_masks: list[pd.Series] = []
        short_masks: list[pd.Series] = []
        long_exits: list[pd.Series] = []
        short_exits: list[pd.Series] = []

        for indicator in strategy.indicators:
            indicator_masks = self._indicator_masks(frame, strategy, indicator)
            long_masks.append(indicator_masks["long"])
            short_masks.append(indicator_masks["short"])
            long_exits.append(indicator_masks["exit_long"])
            short_exits.append(indicator_masks["exit_short"])

        long_score = sum(mask.fillna(False).astype(int) for mask in long_masks) if long_masks else pd.Series(0, index=frame.index)
        short_score = sum(mask.fillna(False).astype(int) for mask in short_masks) if short_masks else pd.Series(0, index=frame.index)
        required = self._required_score(len(strategy.indicators), strategy.archetype)

        long_candidate = long_score >= required
        short_candidate = short_score >= required

        if strategy.archetype == "momentum_breakout":
            rolling_high = frame["high"].rolling(20).max().shift(1)
            rolling_low = frame["low"].rolling(20).min().shift(1)
            long_candidate &= frame["close"] > rolling_high
            short_candidate &= frame["close"] < rolling_low
        elif strategy.archetype == "mean_reversion":
            long_trigger = pd.Series(False, index=frame.index)
            short_trigger = pd.Series(False, index=frame.index)
            if "rsi" in strategy.indicators and "rsi" in frame.columns:
                long_trigger |= frame["rsi"] < float(strategy.parameters.get("rsi", {}).get("lower_threshold", 30))
                short_trigger |= frame["rsi"] > float(strategy.parameters.get("rsi", {}).get("upper_threshold", 70))
            if "bollinger_bands" in strategy.indicators and {"bb_lower", "bb_upper"} <= set(frame.columns):
                long_trigger |= frame["close"] <= frame["bb_lower"]
                short_trigger |= frame["close"] >= frame["bb_upper"]
            if "stochastic" in strategy.indicators and {"stoch_k", "stoch_d"} <= set(frame.columns):
                long_trigger |= (frame["stoch_k"] < 20) & (frame["stoch_k"] > frame["stoch_d"])
                short_trigger |= (frame["stoch_k"] > 80) & (frame["stoch_k"] < frame["stoch_d"])
            if "cci" in strategy.indicators and "cci" in frame.columns:
                long_trigger |= frame["cci"] < -100
                short_trigger |= frame["cci"] > 100
            long_candidate &= long_trigger
            short_candidate &= short_trigger
        elif strategy.archetype == "crossover_confirmation":
            cross_long, cross_short = self._primary_cross_masks(frame, strategy.indicators)
            long_candidate &= cross_long
            short_candidate &= cross_short
        elif strategy.archetype == "pullback_reentry":
            if "ema_fast" in frame.columns:
                anchor = frame["ema_fast"]
            elif "sma_fast" in frame.columns:
                anchor = frame["sma_fast"]
            else:
                anchor = frame["close"]
            tolerance = 0.003
            long_candidate &= frame["close"] <= anchor * (1 + tolerance)
            short_candidate &= frame["close"] >= anchor * (1 - tolerance)
        elif strategy.archetype == "volatility_expansion":
            vol_filter = pd.Series(False, index=frame.index)
            if "atr_pct" in frame.columns and "atr_ma" in frame.columns:
                vol_filter |= frame["atr_pct"] > frame["atr_ma"]
            if "adx" in frame.columns:
                threshold = float(strategy.parameters.get("adx", {}).get("threshold", 20))
                vol_filter |= frame["adx"] > threshold
            long_candidate &= vol_filter
            short_candidate &= vol_filter

        long_candidate = long_candidate.fillna(False).astype(bool)
        short_candidate = short_candidate.fillna(False).astype(bool)
        long_entry = long_candidate & ~long_candidate.shift(1, fill_value=False)
        short_entry = short_candidate & ~short_candidate.shift(1, fill_value=False)
        exit_long = self._combine_exit_masks(long_exits, long_candidate, short_entry)
        exit_short = self._combine_exit_masks(short_exits, short_candidate, long_entry)

        entry_signal = np.where(long_entry, 1, np.where(short_entry, -1, 0))
        exit_signal = np.where(exit_long, -1, np.where(exit_short, 1, 0))

        local = frame.copy()
        local["entry_signal"] = pd.Series(entry_signal, index=frame.index).astype(int)
        local["exit_signal"] = pd.Series(exit_signal, index=frame.index).astype(int)
        return local

    @staticmethod
    def _required_score(indicator_count: int, archetype: str) -> int:
        if archetype == "mean_reversion":
            return max(1, indicator_count // 2)
        return max(2, int(np.ceil(indicator_count * 0.67)))

    @staticmethod
    def _combine_exit_masks(
        exit_masks: list[pd.Series],
        candidate_mask: pd.Series,
        opposite_entry: pd.Series,
    ) -> pd.Series:
        candidate_mask = candidate_mask.fillna(False).astype(bool)
        opposite_entry = opposite_entry.fillna(False).astype(bool)
        if exit_masks:
            combined = exit_masks[0].fillna(False).astype(bool)
            for mask in exit_masks[1:]:
                combined |= mask.fillna(False).astype(bool)
        else:
            combined = pd.Series(False, index=candidate_mask.index)
        combined |= opposite_entry
        combined |= (~candidate_mask) & candidate_mask.shift(1, fill_value=False)
        return combined

    @staticmethod
    def _primary_cross_masks(frame: pd.DataFrame, indicators: list[str]) -> tuple[pd.Series, pd.Series]:
        if "ema" in indicators and {"ema_fast", "ema_slow"} <= set(frame.columns):
            long_mask = (frame["ema_fast"] > frame["ema_slow"]) & (frame["ema_fast"].shift(1) <= frame["ema_slow"].shift(1))
            short_mask = (frame["ema_fast"] < frame["ema_slow"]) & (frame["ema_fast"].shift(1) >= frame["ema_slow"].shift(1))
            return long_mask.fillna(False), short_mask.fillna(False)
        if "sma" in indicators and {"sma_fast", "sma_slow"} <= set(frame.columns):
            long_mask = (frame["sma_fast"] > frame["sma_slow"]) & (frame["sma_fast"].shift(1) <= frame["sma_slow"].shift(1))
            short_mask = (frame["sma_fast"] < frame["sma_slow"]) & (frame["sma_fast"].shift(1) >= frame["sma_slow"].shift(1))
            return long_mask.fillna(False), short_mask.fillna(False)
        if "macd" in indicators and {"macd_line", "macd_signal"} <= set(frame.columns):
            long_mask = (frame["macd_line"] > frame["macd_signal"]) & (
                frame["macd_line"].shift(1) <= frame["macd_signal"].shift(1)
            )
            short_mask = (frame["macd_line"] < frame["macd_signal"]) & (
                frame["macd_line"].shift(1) >= frame["macd_signal"].shift(1)
            )
            return long_mask.fillna(False), short_mask.fillna(False)
        false_mask = pd.Series(False, index=frame.index)
        return false_mask, false_mask

    def _indicator_masks(self, frame: pd.DataFrame, strategy: StrategyDefinition, indicator: str) -> dict[str, pd.Series]:
        false_mask = pd.Series(False, index=frame.index)

        if indicator == "sma" and {"sma_fast", "sma_slow"} <= set(frame.columns):
            long_mask = (frame["sma_fast"] > frame["sma_slow"]) & (frame["close"] > frame["sma_slow"])
            short_mask = (frame["sma_fast"] < frame["sma_slow"]) & (frame["close"] < frame["sma_slow"])
            return {"long": long_mask, "short": short_mask, "exit_long": short_mask, "exit_short": long_mask}

        if indicator == "ema" and {"ema_fast", "ema_slow"} <= set(frame.columns):
            long_mask = (frame["ema_fast"] > frame["ema_slow"]) & (frame["close"] > frame["ema_fast"])
            short_mask = (frame["ema_fast"] < frame["ema_slow"]) & (frame["close"] < frame["ema_fast"])
            return {"long": long_mask, "short": short_mask, "exit_long": short_mask, "exit_short": long_mask}

        if indicator == "rsi" and "rsi" in frame.columns:
            upper = float(strategy.parameters.get("rsi", {}).get("upper_threshold", 70))
            lower = float(strategy.parameters.get("rsi", {}).get("lower_threshold", 30))
            if strategy.archetype == "mean_reversion":
                long_mask = frame["rsi"] < lower
                short_mask = frame["rsi"] > upper
                exit_long = frame["rsi"] > 50
                exit_short = frame["rsi"] < 50
            else:
                long_mask = frame["rsi"] > 55
                short_mask = frame["rsi"] < 45
                exit_long = frame["rsi"] < 45
                exit_short = frame["rsi"] > 55
            return {"long": long_mask, "short": short_mask, "exit_long": exit_long, "exit_short": exit_short}

        if indicator == "macd" and {"macd_line", "macd_signal", "macd_hist"} <= set(frame.columns):
            long_mask = (frame["macd_line"] > frame["macd_signal"]) & (frame["macd_hist"] > 0)
            short_mask = (frame["macd_line"] < frame["macd_signal"]) & (frame["macd_hist"] < 0)
            return {"long": long_mask, "short": short_mask, "exit_long": short_mask, "exit_short": long_mask}

        if indicator == "atr" and {"atr_pct", "atr_ma"} <= set(frame.columns):
            vol_mask = frame["atr_pct"] > frame["atr_ma"]
            return {"long": vol_mask, "short": vol_mask, "exit_long": ~vol_mask, "exit_short": ~vol_mask}

        if indicator == "vwap" and "vwap" in frame.columns:
            long_mask = frame["close"] > frame["vwap"]
            short_mask = frame["close"] < frame["vwap"]
            return {"long": long_mask, "short": short_mask, "exit_long": short_mask, "exit_short": long_mask}

        if indicator == "bollinger_bands" and {"bb_mid", "bb_upper", "bb_lower"} <= set(frame.columns):
            if strategy.archetype == "mean_reversion":
                long_mask = frame["close"] <= frame["bb_lower"]
                short_mask = frame["close"] >= frame["bb_upper"]
                exit_long = frame["close"] >= frame["bb_mid"]
                exit_short = frame["close"] <= frame["bb_mid"]
            else:
                long_mask = frame["close"] > frame["bb_mid"]
                short_mask = frame["close"] < frame["bb_mid"]
                exit_long = frame["close"] < frame["bb_mid"]
                exit_short = frame["close"] > frame["bb_mid"]
            return {"long": long_mask, "short": short_mask, "exit_long": exit_long, "exit_short": exit_short}

        if indicator == "stochastic" and {"stoch_k", "stoch_d"} <= set(frame.columns):
            if strategy.archetype == "mean_reversion":
                long_mask = (frame["stoch_k"] < 20) & (frame["stoch_k"] > frame["stoch_d"])
                short_mask = (frame["stoch_k"] > 80) & (frame["stoch_k"] < frame["stoch_d"])
            else:
                long_mask = (frame["stoch_k"] > frame["stoch_d"]) & (frame["stoch_k"] > 40)
                short_mask = (frame["stoch_k"] < frame["stoch_d"]) & (frame["stoch_k"] < 60)
            return {"long": long_mask, "short": short_mask, "exit_long": short_mask, "exit_short": long_mask}

        if indicator == "adx" and {"adx", "plus_di", "minus_di"} <= set(frame.columns):
            threshold = float(strategy.parameters.get("adx", {}).get("threshold", 20))
            long_mask = (frame["adx"] > threshold) & (frame["plus_di"] > frame["minus_di"])
            short_mask = (frame["adx"] > threshold) & (frame["minus_di"] > frame["plus_di"])
            weak_trend = frame["adx"] < threshold
            return {"long": long_mask, "short": short_mask, "exit_long": weak_trend | short_mask, "exit_short": weak_trend | long_mask}

        if indicator == "momentum" and "momentum" in frame.columns:
            long_mask = frame["momentum"] > 0
            short_mask = frame["momentum"] < 0
            return {"long": long_mask, "short": short_mask, "exit_long": short_mask, "exit_short": long_mask}

        if indicator == "roc" and "roc" in frame.columns:
            long_mask = frame["roc"] > 0
            short_mask = frame["roc"] < 0
            return {"long": long_mask, "short": short_mask, "exit_long": short_mask, "exit_short": long_mask}

        if indicator == "volume_sma" and "volume_sma" in frame.columns:
            multiplier = float(strategy.parameters.get("volume_sma", {}).get("multiplier", 1.1))
            vol_ok = frame["volume"] > (frame["volume_sma"] * multiplier)
            long_mask = vol_ok & (frame["close"] >= frame["open"])
            short_mask = vol_ok & (frame["close"] <= frame["open"])
            return {"long": long_mask, "short": short_mask, "exit_long": false_mask, "exit_short": false_mask}

        if indicator == "obv" and {"obv", "obv_signal"} <= set(frame.columns):
            long_mask = frame["obv"] > frame["obv_signal"]
            short_mask = frame["obv"] < frame["obv_signal"]
            return {"long": long_mask, "short": short_mask, "exit_long": short_mask, "exit_short": long_mask}

        if indicator == "cci" and "cci" in frame.columns:
            if strategy.archetype == "mean_reversion":
                long_mask = frame["cci"] < -100
                short_mask = frame["cci"] > 100
            else:
                long_mask = frame["cci"] > 0
                short_mask = frame["cci"] < 0
            return {"long": long_mask, "short": short_mask, "exit_long": short_mask, "exit_short": long_mask}

        return {"long": false_mask, "short": false_mask, "exit_long": false_mask, "exit_short": false_mask}

    def _execute_backtest(self, signal_frame: pd.DataFrame, settings: EngineSettings) -> pd.DataFrame:
        trades: list[dict[str, Any]] = []

        for ticker, ticker_df in signal_frame.groupby("ticker", sort=False):
            session_groups = ticker_df.groupby(ticker_df["datetime"].dt.date, sort=False)
            for _, session_df in session_groups:
                current_position: dict[str, Any] | None = None
                session_df = session_df.sort_values("datetime").reset_index(drop=True)

                for row in session_df.itertuples(index=False):
                    current_time = pd.Timestamp(row.datetime).strftime("%H:%M")

                    if current_position is not None:
                        exit_price, exit_reason = self._exit_decision(row, current_position, settings)
                        if exit_price is not None and exit_reason is not None:
                            trades.append(
                                self._close_trade(ticker, row.datetime, exit_price, exit_reason, current_position, settings)
                            )
                            current_position = None

                    if current_position is None and current_time <= settings.no_entry_after and int(row.entry_signal) in (-1, 1):
                        side = int(row.entry_signal)
                        entry_price = float(row.close) * (1 + settings.slippage_rate if side == 1 else 1 - settings.slippage_rate)
                        quantity = max(int(settings.capital_per_trade // max(entry_price, 1e-9)), 1)
                        current_position = {
                            "entry_time": row.datetime,
                            "entry_price": entry_price,
                            "quantity": quantity,
                            "side": side,
                        }

                if current_position is not None:
                    last_row = session_df.iloc[-1]
                    exit_price = float(last_row["close"]) * (
                        1 - settings.slippage_rate if current_position["side"] == 1 else 1 + settings.slippage_rate
                    )
                    trades.append(
                        self._close_trade(
                            ticker=ticker,
                            exit_time=last_row["datetime"],
                            exit_price=exit_price,
                            exit_reason="EOD",
                            current_position=current_position,
                            settings=settings,
                        )
                    )

        tradebook = pd.DataFrame(trades, columns=EMPTY_TRADEBOOK_COLUMNS)
        if tradebook.empty:
            return self._empty_tradebook()
        tradebook["entry_time"] = pd.to_datetime(tradebook["entry_time"])
        tradebook["exit_time"] = pd.to_datetime(tradebook["exit_time"])
        return tradebook.sort_values(["exit_time", "ticker"]).reset_index(drop=True)

    def _exit_decision(
        self,
        row,
        current_position: dict[str, Any],
        settings: EngineSettings,
    ) -> tuple[float | None, str | None]:
        side = current_position["side"]
        entry_price = current_position["entry_price"]
        current_time = pd.Timestamp(row.datetime).strftime("%H:%M")

        stop_price = entry_price * (1 - settings.stop_loss_pct) if side == 1 else entry_price * (1 + settings.stop_loss_pct)
        target_price = entry_price * (1 + settings.take_profit_pct) if side == 1 else entry_price * (1 - settings.take_profit_pct)

        if side == 1 and float(row.low) <= stop_price:
            return stop_price, "SL"
        if side == -1 and float(row.high) >= stop_price:
            return stop_price, "SL"
        if side == 1 and float(row.high) >= target_price:
            return target_price, "TP"
        if side == -1 and float(row.low) <= target_price:
            return target_price, "TP"
        if int(row.exit_signal) == -side:
            exit_price = float(row.close) * (1 - settings.slippage_rate if side == 1 else 1 + settings.slippage_rate)
            return exit_price, "SIGNAL"
        if current_time >= settings.eod_exit_time:
            exit_price = float(row.close) * (1 - settings.slippage_rate if side == 1 else 1 + settings.slippage_rate)
            return exit_price, "EOD"
        return None, None

    def _close_trade(
        self,
        ticker: str,
        exit_time,
        exit_price: float,
        exit_reason: str,
        current_position: dict[str, Any],
        settings: EngineSettings,
    ) -> dict[str, Any]:
        side = current_position["side"]
        quantity = current_position["quantity"]
        gross_pnl = (exit_price - current_position["entry_price"]) * quantity * side
        turnover = (current_position["entry_price"] + exit_price) * quantity
        costs = turnover * settings.brokerage_rate
        pnl = gross_pnl - costs
        return {
            "ticker": ticker,
            "entry_time": current_position["entry_time"],
            "exit_time": exit_time,
            "entry_price": round(current_position["entry_price"], 6),
            "exit_price": round(float(exit_price), 6),
            "position": "long" if side == 1 else "short",
            "quantity": int(quantity),
            "gross_pnl": round(float(gross_pnl), 6),
            "costs": round(float(costs), 6),
            "pnl": round(float(pnl), 6),
            "return_pct": round(float(pnl) / settings.capital_per_trade, 6),
            "exit_reason": exit_reason,
        }

    @staticmethod
    def _build_summary(
        strategy_name: str,
        tradebook: pd.DataFrame,
        market_data: pd.DataFrame,
        job: BacktestJobDefinition | None,
        engine_name: str,
    ) -> StrategyBacktestSummary:
        market_categories = (
            sorted(market_data["dataset_category"].dropna().astype(str).str.lower().unique().tolist())
            if "dataset_category" in market_data.columns
            else []
        )
        if tradebook.empty:
            return StrategyBacktestSummary(
                strategy_name=strategy_name,
                engine_name=engine_name,
                job_id=job.job_id if job else "",
                market_rows=int(len(market_data)),
                tickers_tested=int(market_data["ticker"].nunique()) if "ticker" in market_data.columns else 0,
                market_categories=market_categories,
            )
        return StrategyBacktestSummary(
            strategy_name=strategy_name,
            engine_name=engine_name,
            job_id=job.job_id if job else "",
            trades=int(len(tradebook)),
            gross_pnl=float(tradebook["gross_pnl"].sum()),
            net_pnl=float(tradebook["pnl"].sum()),
            win_rate=float((tradebook["pnl"] > 0).mean()),
            average_trade_pnl=float(tradebook["pnl"].mean()),
            market_rows=int(len(market_data)),
            tickers_tested=int(market_data["ticker"].nunique()) if "ticker" in market_data.columns else 0,
            market_categories=market_categories,
            exit_reasons={key: int(value) for key, value in tradebook["exit_reason"].value_counts().to_dict().items()},
        )

    @staticmethod
    def _empty_market_data() -> pd.DataFrame:
        return pd.DataFrame(
            columns=["datetime", "ticker", "open", "high", "low", "close", "volume", "source_path", "dataset_category"]
        )

    @staticmethod
    def _empty_tradebook() -> pd.DataFrame:
        return pd.DataFrame(columns=EMPTY_TRADEBOOK_COLUMNS)
