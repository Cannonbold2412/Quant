import sqlite3

import pandas as pd
import pytest

from nanoaqrl import data
from nanoaqrl._lib.vault import (
    VaultAccessDeniedError,
    VaultConfig,
    VaultGuard,
    VaultLockedError,
    open_vault_for_promotion,
)


def _memory_conn():
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE vault_access_log (id INTEGER PRIMARY KEY, family TEXT, reason TEXT, opened_at TEXT)"
    )
    return conn


def test_guard_blocks_date_range_overlap():
    config = VaultConfig(locked_start=pd.Timestamp("2023-01-01"), locked_end=pd.Timestamp("2023-12-31"))
    guard = VaultGuard(config)
    with pytest.raises(VaultLockedError):
        guard.check("X", pd.Timestamp("2022-06-01"), pd.Timestamp("2023-06-01"))


def test_guard_allows_ranges_outside_the_vault():
    config = VaultConfig(locked_start=pd.Timestamp("2023-01-01"), locked_end=pd.Timestamp("2023-12-31"))
    guard = VaultGuard(config)
    guard.check("X", pd.Timestamp("2020-01-01"), pd.Timestamp("2021-01-01"))  # must not raise


def test_guard_blocks_locked_instruments_regardless_of_date():
    config = VaultConfig(
        locked_start=pd.Timestamp("2099-01-01"), locked_end=pd.Timestamp("2099-12-31"),
        locked_instruments=frozenset({"SECRET"}),
    )
    guard = VaultGuard(config)
    with pytest.raises(VaultLockedError):
        guard.check("SECRET", pd.Timestamp("2000-01-01"), pd.Timestamp("2000-02-01"))


def test_open_once_reverts_after_the_with_block():
    config = VaultConfig(locked_start=pd.Timestamp("2023-01-01"), locked_end=pd.Timestamp("2023-12-31"))
    guard = VaultGuard(config)
    with guard.open_once("family_a"):
        guard.check("X", pd.Timestamp("2023-06-01"), pd.Timestamp("2023-07-01"))  # allowed while open
    with pytest.raises(VaultLockedError):
        guard.check("X", pd.Timestamp("2023-06-01"), pd.Timestamp("2023-07-01"))  # locked again


def test_promotion_open_is_logged_and_budgeted():
    config = VaultConfig(locked_start=pd.Timestamp("2023-01-01"), locked_end=pd.Timestamp("2023-12-31"))
    guard = VaultGuard(config)
    conn = _memory_conn()

    with open_vault_for_promotion(guard, conn, "family_a", "promotion review", lifetime_budget=1):
        guard.check("X", pd.Timestamp("2023-06-01"), pd.Timestamp("2023-07-01"))

    row = conn.execute("SELECT COUNT(*) FROM vault_access_log WHERE family = 'family_a'").fetchone()
    assert row[0] == 1

    with pytest.raises(VaultAccessDeniedError):
        with open_vault_for_promotion(guard, conn, "family_a", "second attempt", lifetime_budget=1):
            pass


def test_data_module_get_ohlcv_refuses_the_vaulted_range():
    locked_start, locked_end = data.VAULT_CONFIG.locked_start, data.VAULT_CONFIG.locked_end
    with pytest.raises(data.VaultLockedError):
        data.get_ohlcv(data.INSTRUMENT, locked_start, locked_end)


def test_data_module_serves_the_research_range_fine():
    start, end = data.research_range()
    df = data.get_ohlcv(data.INSTRUMENT, start, end)
    assert len(df) > 0
