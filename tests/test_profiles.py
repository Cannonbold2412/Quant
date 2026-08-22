"""Profiles: validation, deterministic hashing, and derived annualisation.

TRD §13.2 on `periods_per_year`: *"Must come from the profile — never a
hardcoded constant. One wrong value makes every Sharpe in the database
fiction."* These tests are the guard on that claim.
"""
from __future__ import annotations

import ast
import math
from pathlib import Path

import pytest
from pydantic import ValidationError

from aqrl.config import PROJECT_ROOT
from aqrl.db.repositories import CostModelRepository, MarketProfileRepository
from aqrl.profiles import ProfileError, ProfileLoader, hash_profile, resolve_periods_per_year

AQRL_PACKAGE = PROJECT_ROOT / "aqrl"

# NSE trades 09:15-15:30 = 22,500 seconds across 252 sessions.
EXPECTED_PERIODS = {
    "1sec": 5_670_000,      # 252 x 22,500
    "1min": 94_500,         # 252 x 375
    "15min": 6_300,         # 252 x 25
    "1hour": 1_575,         # 252 x 6.25 — note the fraction
    "daily": 252,
    "weekly": 52.1786,      # 365.25 / 7
    "monthly": 12.0,        # 365.25 / 12 days per bar
}


@pytest.mark.parametrize("timeframe,expected", EXPECTED_PERIODS.items())
def test_periods_per_year_is_derived_correctly(loader, nse_market, timeframe, expected):
    derived = resolve_periods_per_year(nse_market, loader.load_timeframe(timeframe))
    assert math.isclose(derived, expected, rel_tol=1e-3), f"{timeframe}: {derived} != {expected}"


def test_the_full_timeframe_range_loads(loader):
    """The architecture must span 1 second to 1 month (TRD §13.2)."""
    bar_seconds = {name: loader.load_timeframe(name).bar_seconds for name in loader.list_timeframes()}
    assert min(bar_seconds.values()) == 1
    assert max(bar_seconds.values()) >= 2_592_000  # ~1 month


def test_sub_minute_timeframes_must_carry_a_fidelity_warning(loader):
    """Supported is not the same as trustworthy (TRD §13.2)."""
    assert loader.load_timeframe("1sec").fidelity_warning
    assert loader.load_timeframe("daily").fidelity_warning is None


def test_asserted_periods_per_year_must_match_the_calendar(loader, nse_market, tmp_path: Path):
    """A drifted assertion fails loudly rather than silently rescaling Sharpes."""
    import yaml

    source = (PROJECT_ROOT / "profiles" / "timeframes" / "daily.yaml").read_text()
    payload = yaml.safe_load(source)
    payload["periods_per_year_assertions"]["nse_equity"] = 365  # wrong on purpose

    root = tmp_path / "profiles"
    (root / "timeframes").mkdir(parents=True)
    (root / "markets").mkdir()
    (root / "costs").mkdir()
    (root / "timeframes" / "daily.yaml").write_text(yaml.safe_dump(payload))
    for name in ("markets/nse_equity.yaml", "costs/nse_equity.cash_equity.yaml"):
        (root / name).write_text((PROJECT_ROOT / "profiles" / name).read_text())

    with pytest.raises(ValueError, match="periods_per_year mismatch"):
        ProfileLoader(root).resolve("nse_equity", "daily", "cash_equity")


def test_profile_hash_is_deterministic_across_loads(loader):
    first = ProfileLoader(loader.root).load_market("nse_equity")
    second = ProfileLoader(loader.root).load_market("nse_equity")
    assert hash_profile(first) == hash_profile(second)


def test_profile_hash_changes_when_content_changes(loader):
    profile = loader.load_market("nse_equity")
    mutated = profile.model_copy(update={"version": "9.9.9"})
    assert hash_profile(mutated) != hash_profile(profile)


def test_profiles_are_immutable(loader):
    """A profile change is a new version with a new hash, never a mutation."""
    profile = loader.load_market("nse_equity")
    with pytest.raises(ValidationError):
        profile.version = "2.0.0"  # type: ignore[misc]


def test_missing_profile_names_what_is_available(loader):
    with pytest.raises(ProfileError, match="available"):
        loader.load_market("does_not_exist")


def test_malformed_yaml_is_rejected(tmp_path: Path):
    root = tmp_path / "profiles"
    (root / "markets").mkdir(parents=True)
    (root / "markets" / "broken.yaml").write_text("name: broken\nversion: 1\n")  # missing everything
    with pytest.raises(ProfileError, match="failed validation"):
        ProfileLoader(root).load_market("broken")


def test_resolve_rejects_an_undeclared_asset_class(loader):
    with pytest.raises(ProfileError, match="does not declare asset class"):
        loader.resolve("nse_equity", "daily", "perpetual")


def test_nse_cash_equity_round_trip_matches_the_documented_figure(loader):
    """TRD §6.3: ~22 bps statutory, ~27-32 bps all-in."""
    cost = loader.load_cost_model("nse_equity", "cash_equity")
    round_trip = cost.round_trip_bps()
    assert 27.0 <= round_trip <= 32.0, round_trip
    # The 2x stress the bar applies.
    assert 55.0 <= round_trip * 2 <= 65.0


def test_cost_model_is_flagged_as_needing_a_contract_note(loader):
    cost = loader.load_cost_model("nse_equity", "cash_equity")
    assert cost.requires_contract_note_verification is True
    assert cost.source


def test_placeholder_cost_models_are_flagged(loader):
    assert loader.load_cost_model("nse_equity", "etf").is_placeholder is True


def test_registration_is_idempotent_on_the_hash(conn, loader):
    first = loader.register(conn)
    markets = MarketProfileRepository(conn)
    count_after_first = markets.count()
    second = loader.register(conn)
    assert first == second
    assert markets.count() == count_after_first, "re-registering unchanged YAML created a duplicate"


def test_registered_hash_matches_the_resolved_hash(conn, loader):
    loader.register(conn)
    resolved = loader.resolve("nse_equity", "daily", "cash_equity")
    assert MarketProfileRepository(conn).by_hash(resolved.market_profile_hash) is not None
    assert CostModelRepository(conn).by_hash(resolved.cost_model_hash) is not None


def test_annualisation_factor_is_the_square_root(loader):
    resolved = loader.resolve("nse_equity", "daily", "cash_equity")
    assert math.isclose(resolved.annualisation_factor, math.sqrt(252))


def test_no_hardcoded_annualisation_constant_in_the_package():
    """The constant that used to live in nanoaqrl/data.py must not come back.

    TRD §13.2 calls a hardcoded value here a project-level bug: 252 is right for
    daily NSE bars and wrong for every other combination the architecture
    supports.
    """
    # Walk the AST rather than the text, so prose in docstrings explaining the
    # derivation does not read as an implementation constant.
    # Only values that are diagnostic of annualisation. 12 and 52 are excluded
    # deliberately: they are far too common as ordinary integers (string slice
    # widths, divisors) for their presence to mean anything.
    suspicious = {252, 365, 94_500, 5_670_000, 525_600}
    # Genuinely calendar arithmetic, not annualisation: seconds in a day and
    # days in a year, both used to *derive* the value from a profile.
    permitted = {86_400, 365.25}

    offenders = []
    for path in AQRL_PACKAGE.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, (int, float))
                and not isinstance(node.value, bool)
                and node.value in suspicious
                and node.value not in permitted
            ):
                offenders.append(f"{path.relative_to(PROJECT_ROOT)}:{node.lineno}: {node.value}")
    assert not offenders, (
        "hardcoded annualisation constants in aqrl/ — these must come from a "
        "resolved profile:\n" + "\n".join(offenders)
    )
