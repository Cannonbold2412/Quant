"""The vault — data the loop cannot read (TRD §15.2).

Every other protection depends on honestly counting trials. The vault is the
one defence that doesn't: a locked span of history and/or instrument set that
`evaluate.py`'s normal path structurally cannot reach. **Not "should not" —
"cannot."** `evaluate.py` and `strategy.py` only ever call `data.get_ohlcv()`
(see `data.py`), which always checks the guard and raises. The *only* function
that can ever open the vault — `open_vault_for_promotion` — is never imported
by `evaluate.py`; it exists for the Stage 9 human promotion gate, which does
not run inside the automated research loop.

Opening the vault is logged and decremented against a lifetime budget per
strategy family (TRD §15.2) — tracked in `db.py`'s `vault_access_log` table.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass

import pandas as pd


class VaultLockedError(RuntimeError):
    """Raised whenever the research loop's normal read path touches
    vault-locked data. There is no research-loop code path that can catch
    and suppress this into an open state."""


class VaultAccessDeniedError(RuntimeError):
    """Raised when a promotion-time open would exceed the family's lifetime
    budget."""


@dataclass(frozen=True)
class VaultConfig:
    locked_start: pd.Timestamp
    locked_end: pd.Timestamp
    locked_instruments: frozenset = frozenset()


def intersects_vault(instrument: str, start: pd.Timestamp, end: pd.Timestamp, config: VaultConfig) -> bool:
    date_overlap = not (end < config.locked_start or start > config.locked_end)
    instrument_locked = instrument in config.locked_instruments
    return date_overlap or instrument_locked


class VaultGuard:
    """Defaults closed. Only `open_once()` can flip it, and only for the
    duration of its `with` block — access reverts to locked the instant the
    block exits, so a single promotion-time peek can never quietly become a
    standing exemption."""

    def __init__(self, config: VaultConfig):
        self._config = config
        self._open_family: str | None = None

    def check(self, instrument: str, start: pd.Timestamp, end: pd.Timestamp) -> None:
        if self._open_family is not None:
            return
        if intersects_vault(instrument, start, end, self._config):
            raise VaultLockedError(
                f"{instrument} [{start} - {end}] is inside the vault; the research loop has no read path to it"
            )

    @contextmanager
    def open_once(self, family: str):
        self._open_family = family
        try:
            yield
        finally:
            self._open_family = None


def get_remaining_budget(conn, family: str, lifetime_budget: int) -> int:
    row = conn.execute("SELECT COUNT(*) FROM vault_access_log WHERE family = ?", (family,)).fetchone()
    used = row[0] if row else 0
    return lifetime_budget - used


def log_vault_access(conn, family: str, reason: str) -> None:
    conn.execute(
        "INSERT INTO vault_access_log (family, reason, opened_at) VALUES (?, ?, datetime('now'))",
        (family, reason),
    )
    conn.commit()


@contextmanager
def open_vault_for_promotion(guard: VaultGuard, conn, family: str, reason: str, lifetime_budget: int):
    """The Stage 9 promotion-gate path. Never imported by `evaluate.py` or
    `strategy.py` — only a human-gated promotion step would call this, and
    nanoAQRL has no such step wired in yet (that's Stage 9)."""
    remaining = get_remaining_budget(conn, family, lifetime_budget)
    if remaining <= 0:
        raise VaultAccessDeniedError(
            f"family '{family}' has exhausted its vault budget ({lifetime_budget}); "
            "cannot be promoted again until genuinely new data exists"
        )
    log_vault_access(conn, family, reason)
    with guard.open_once(family):
        yield
