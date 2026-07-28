"""YAML -> validated object -> content hash -> registry.

Generic before specific. Implementation_Plan §3 is explicit that market and
timeframe are *not* fixed to one pilot — all six markets are in scope from the
start, so **profile loading must be generic before any single profile is filled
in**. Nothing in this module knows that `nse_equity` exists; it discovers
whatever YAML is on disk.

    profiles/
      markets/nse_equity.yaml
      timeframes/daily.yaml
      costs/nse_equity.cash_equity.yaml
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from ..hashing import content_hash
from .models import (
    CostModel,
    MarketProfile,
    ResolvedProfile,
    TimeframeProfile,
    check_periods_per_year,
    profile_payload,
)


class ProfileError(LookupError):
    """A profile is missing, malformed, or internally inconsistent."""


def _read_yaml(path: Path) -> dict[str, Any]:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ProfileError(f"{path.name} is not valid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise ProfileError(f"{path.name} must contain a YAML mapping, got {type(data).__name__}")
    return data


class ProfileLoader:
    """Loads and hashes profiles from a directory tree."""

    def __init__(self, profiles_dir: Path | str | None = None) -> None:
        if profiles_dir is None:
            from ..config import get_settings

            profiles_dir = get_settings().profiles_dir
        self.root = Path(profiles_dir)

    # -- discovery -------------------------------------------------------------

    def _names(self, subdir: str) -> list[str]:
        directory = self.root / subdir
        if not directory.exists():
            return []
        return sorted(path.stem for path in directory.glob("*.yaml"))

    def list_markets(self) -> list[str]:
        return self._names("markets")

    def list_timeframes(self) -> list[str]:
        return self._names("timeframes")

    def list_cost_models(self) -> list[tuple[str, str]]:
        pairs = []
        for stem in self._names("costs"):
            market, _, asset_class = stem.partition(".")
            if not asset_class:
                raise ProfileError(f"cost model filename must be <market>.<asset_class>.yaml, got {stem}.yaml")
            pairs.append((market, asset_class))
        return pairs

    # -- loading ---------------------------------------------------------------

    def _load(self, subdir: str, stem: str, model: type, label: str) -> Any:
        path = self.root / subdir / f"{stem}.yaml"
        if not path.exists():
            available = ", ".join(self._names(subdir)) or "none"
            raise ProfileError(f"no {label} profile {stem!r} in {self.root / subdir} (available: {available})")
        try:
            return model(**_read_yaml(path))
        except ProfileError:
            raise
        except Exception as exc:  # pydantic ValidationError and friends
            raise ProfileError(f"{path.name} failed validation:\n{exc}") from exc

    def load_market(self, name: str) -> MarketProfile:
        profile = self._load("markets", name, MarketProfile, "market")
        if profile.name != name:
            raise ProfileError(f"markets/{name}.yaml declares name={profile.name!r}; filename and name must agree")
        return profile

    def load_timeframe(self, name: str) -> TimeframeProfile:
        profile = self._load("timeframes", name, TimeframeProfile, "timeframe")
        if profile.name != name:
            raise ProfileError(
                f"timeframes/{name}.yaml declares name={profile.name!r}; filename and name must agree"
            )
        return profile

    def load_cost_model(self, market: str, asset_class: str) -> CostModel:
        """Cost is keyed on the pair (TRD §6.3) — never market alone."""
        model = self._load("costs", f"{market}.{asset_class}", CostModel, "cost")
        if (model.market, model.asset_class) != (market, asset_class):
            raise ProfileError(
                f"costs/{market}.{asset_class}.yaml declares ({model.market}, {model.asset_class}); "
                "filename and contents must agree"
            )
        return model

    # -- resolution ------------------------------------------------------------

    def resolve(self, market: str, timeframe: str, asset_class: str) -> ResolvedProfile:
        """Resolve the triple an experiment is actually run under.

        `periods_per_year` is derived here from the market calendar and the bar
        size, and cross-checked against any assertion the timeframe declared.
        Nothing downstream may hardcode an annualisation constant.
        """
        market_profile = self.load_market(market)
        timeframe_profile = self.load_timeframe(timeframe)

        if asset_class not in market_profile.asset_classes:
            raise ProfileError(
                f"{market} does not declare asset class {asset_class!r} "
                f"(declared: {', '.join(market_profile.asset_classes)})"
            )

        cost_model = self.load_cost_model(market, asset_class)
        if cost_model.currency != market_profile.reference.currency:
            raise ProfileError(
                f"cost model currency {cost_model.currency} does not match "
                f"{market}'s P&L currency {market_profile.reference.currency}"
            )

        periods_per_year = check_periods_per_year(market_profile, timeframe_profile)

        return ResolvedProfile(
            market=market_profile,
            timeframe=timeframe_profile,
            cost_model=cost_model,
            periods_per_year=periods_per_year,
            market_profile_hash=hash_profile(market_profile),
            timeframe_profile_hash=hash_profile(timeframe_profile),
            cost_model_hash=hash_profile(cost_model),
        )

    # -- registry --------------------------------------------------------------

    def register(self, conn: Any) -> dict[str, int]:
        """Write every discovered profile into the database registries.

        Idempotent on the content hash: re-registering unchanged YAML is a
        no-op, so an experiment's pinned hash keeps meaning the same bytes.
        """
        from ..db.repositories import (
            CostModelRepository,
            MarketProfileRepository,
            TimeframeProfileRepository,
        )

        counts = {"markets": 0, "timeframes": 0, "cost_models": 0}

        markets = MarketProfileRepository(conn)
        for name in self.list_markets():
            profile = self.load_market(name)
            markets.register(name, profile.version, profile_payload(profile), hash_profile(profile))
            counts["markets"] += 1

        timeframes = TimeframeProfileRepository(conn)
        for name in self.list_timeframes():
            profile = self.load_timeframe(name)
            timeframes.register(name, profile.version, profile_payload(profile), hash_profile(profile))
            counts["timeframes"] += 1

        costs = CostModelRepository(conn)
        for market, asset_class in self.list_cost_models():
            model = self.load_cost_model(market, asset_class)
            costs.register(
                market, asset_class, model.version, profile_payload(model), hash_profile(model)
            )
            counts["cost_models"] += 1

        return counts


def hash_profile(profile: Any) -> str:
    """The content hash an experiment pins (TRD §6.6)."""
    return content_hash(profile_payload(profile))


@lru_cache(maxsize=1)
def default_loader() -> ProfileLoader:
    return ProfileLoader()
