from __future__ import annotations

from copy import deepcopy
from itertools import combinations
from typing import Any

from config import AppConfig
from schemas import IndicatorLibraryEntry, MIN_STRATEGY_INDICATORS, StrategyDefinition
from utils.indicator_registry import indicator_metadata


class StrategyGenerator:
    def __init__(self, config: AppConfig) -> None:
        self.config = config

    def generate(self, library: list[IndicatorLibraryEntry]) -> list[StrategyDefinition]:
        supported = [entry for entry in library if entry.supported_in_engine]
        supported.sort(key=lambda item: (-item.mention_count, item.name))
        pool = supported[:12]

        target = max(self.config.strategy_target_min, min(self.config.strategy_target_max, max(60, len(pool) * 8)))
        strategies: list[StrategyDefinition] = []
        seen_signatures: set[str] = set()

        for size in range(MIN_STRATEGY_INDICATORS, 5):
            for combo_entries in combinations(pool, size):
                combo = list(combo_entries)
                for archetype in self._compatible_archetypes(combo):
                    strategy = self._build_strategy(combo, archetype, len(strategies) + 1)
                    signature = strategy.signature()
                    if signature in seen_signatures:
                        continue
                    strategies.append(strategy)
                    seen_signatures.add(signature)
                    if len(strategies) >= target:
                        return strategies

        if len(strategies) < self.config.strategy_target_min:
            strategies = self._expand_with_parameter_variants(strategies, seen_signatures)
        return strategies[: self.config.strategy_target_max]

    def _compatible_archetypes(self, combo: list[IndicatorLibraryEntry]) -> list[str]:
        families = {entry.family for entry in combo}
        archetypes: list[str] = []

        if {"trend", "momentum", "trend_strength"} & families:
            archetypes.append("trend_following")
            archetypes.append("crossover_confirmation")
        if {"band", "oscillator"} & families:
            archetypes.append("mean_reversion")
        if {"trend", "oscillator"} <= families or ("trend" in families and "volume" in families):
            archetypes.append("pullback_reentry")
        if "volatility" in families and ({"trend", "momentum", "trend_strength"} & families):
            archetypes.append("volatility_expansion")
        if ({"volume", "momentum"} & families) and ({"trend", "band"} & families):
            archetypes.append("momentum_breakout")

        deduped = list(dict.fromkeys(archetypes))
        return deduped[:2]

    def _build_strategy(
        self,
        combo: list[IndicatorLibraryEntry],
        archetype: str,
        ordinal: int,
        variant_suffix: str = "",
        parameter_overrides: dict[str, dict[str, Any]] | None = None,
    ) -> StrategyDefinition:
        indicator_names = [entry.name for entry in combo]
        parameters = {entry.name: self._parameter_map(entry) for entry in combo}
        for name, overrides in (parameter_overrides or {}).items():
            parameters.setdefault(name, {}).update(overrides)

        name_core = "_".join(indicator_names)
        strategy_name = f"{ordinal:03d}_{archetype}_{name_core}{variant_suffix}"
        entry_conditions, exit_conditions, description = self._build_text(archetype, indicator_names)
        return StrategyDefinition(
            strategy_name=strategy_name,
            archetype=archetype,
            indicators=indicator_names,
            parameters=parameters,
            entry_conditions=entry_conditions,
            exit_conditions=exit_conditions,
            description=description,
            direction="both",
        )

    def _parameter_map(self, entry: IndicatorLibraryEntry) -> dict[str, Any]:
        defaults = deepcopy(indicator_metadata(entry.name)["default_parameters"])
        for parameter in entry.parameters:
            if "=" not in parameter:
                continue
            key, value = parameter.split("=", 1)
            defaults[key.strip()] = self._coerce_value(value.strip())
        return defaults

    @staticmethod
    def _coerce_value(value: str) -> Any:
        if value.lower() in {"true", "false"}:
            return value.lower() == "true"
        try:
            integer = int(value)
            return integer
        except ValueError:
            pass
        try:
            number = float(value)
            return number
        except ValueError:
            return value

    def _build_text(self, archetype: str, indicators: list[str]) -> tuple[list[str], list[str], str]:
        readable = ", ".join(indicators)

        if archetype == "trend_following":
            return (
                [
                    f"Go long when {readable} align in bullish direction with a composite confirmation score above threshold.",
                    "Go short on the symmetric bearish alignment.",
                ],
                [
                    "Exit when the composite alignment flips against the open position.",
                    "Also exit on SL, TP, or EOD from the shared backtester.",
                ],
                f"Trend-following strategy built from {readable}.",
            )
        if archetype == "crossover_confirmation":
            return (
                [
                    f"Enter on fresh crossover or momentum-turn events confirmed by {readable}.",
                    "Only trigger if the confirmation score is strong enough on the signal bar.",
                ],
                [
                    "Exit on opposite crossover or opposite momentum confirmation.",
                    "Risk engine still enforces SL, TP, and EOD exits.",
                ],
                f"Crossover confirmation strategy combining {readable}.",
            )
        if archetype == "mean_reversion":
            return (
                [
                    f"Enter long on oversold or lower-band exhaustion confirmed by {readable}.",
                    "Enter short on mirrored overbought conditions.",
                ],
                [
                    "Exit once price and oscillator state revert toward neutral.",
                    "Also exit on SL, TP, or EOD.",
                ],
                f"Mean-reversion strategy using {readable}.",
            )
        if archetype == "pullback_reentry":
            return (
                [
                    f"Enter in the direction of the dominant trend after a pullback confirmed by {readable}.",
                    "Require enough indicators to re-align after the pullback.",
                ],
                [
                    "Exit when pullback fails and the trend score breaks down.",
                    "Shared engine also manages SL, TP, and EOD exits.",
                ],
                f"Pullback re-entry strategy based on {readable}.",
            )
        if archetype == "volatility_expansion":
            return (
                [
                    f"Enter when trend or momentum alignment is supported by volatility expansion from {readable}.",
                    "Trade both long and short breakouts.",
                ],
                [
                    "Exit when volatility expansion fades or directional alignment flips.",
                    "Risk engine still exits on SL, TP, or EOD.",
                ],
                f"Volatility expansion strategy driven by {readable}.",
            )
        return (
            [
                f"Enter when breakout conditions and confirmation filters from {readable} align.",
                "Use both long and short breakout logic.",
            ],
            [
                "Exit on breakout failure or opposite directional confirmation.",
                "Risk engine still exits on SL, TP, or EOD.",
            ],
            f"Momentum breakout strategy combining {readable}.",
        )

    def _expand_with_parameter_variants(
        self,
        strategies: list[StrategyDefinition],
        seen_signatures: set[str],
    ) -> list[StrategyDefinition]:
        expanded = list(strategies)
        base_strategies = list(strategies)
        variant_index = 1

        while expanded and len(expanded) < self.config.strategy_target_min:
            for base in base_strategies:
                overrides = self._variant_overrides(base, variant_index)
                variant = self._build_strategy(
                    combo=[IndicatorLibraryEntry(name=name, family="", supported_in_engine=True) for name in base.indicators],
                    archetype=base.archetype,
                    ordinal=len(expanded) + 1,
                    variant_suffix=f"_v{variant_index}",
                    parameter_overrides=overrides,
                )
                signature = variant.signature() + f"|v{variant_index}"
                if signature in seen_signatures:
                    continue
                expanded.append(variant)
                seen_signatures.add(signature)
                if len(expanded) >= self.config.strategy_target_min:
                    break
            variant_index += 1
        return expanded

    def _variant_overrides(self, strategy: StrategyDefinition, variant_index: int) -> dict[str, dict[str, Any]]:
        overrides: dict[str, dict[str, Any]] = {}
        for indicator in strategy.indicators:
            metadata = indicator_metadata(indicator)
            params = deepcopy(metadata["default_parameters"])
            tweaked: dict[str, Any] = {}
            for key, value in params.items():
                if isinstance(value, int):
                    tweaked[key] = max(2, value + variant_index)
                elif isinstance(value, float):
                    tweaked[key] = round(value + (0.05 * variant_index), 4)
            if tweaked:
                overrides[indicator] = tweaked
        return overrides
