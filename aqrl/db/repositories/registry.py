"""Registries for content-hashed profiles and cost models (Backend-Schema §13).

An experiment pins a profile by its `config_hash`, so `register` is
**idempotent on the hash**: re-registering identical content returns the
existing row rather than creating a second identity for the same bytes.
Changing the content produces a new hash, hence a new row — and the old
experiments keep pointing at what they were actually scored under.
"""
from __future__ import annotations

from typing import Any

from .base import Repository, Row


class _HashedRegistry(Repository):
    json_columns = frozenset({"config"})

    def by_hash(self, config_hash: str) -> Row | None:
        return self._decode(
            self.conn.execute(
                f"SELECT * FROM {self.table} WHERE config_hash = ?", (config_hash,)
            ).fetchone()
        )

    def _register(self, config_hash: str, **fields: Any) -> int:
        existing = self.by_hash(config_hash)
        if existing is not None:
            return int(existing["id"])
        return self.insert(config_hash=config_hash, **fields)


class MarketProfileRepository(_HashedRegistry):
    table = "market_profiles"

    def register(self, name: str, version: str, config: dict[str, Any], config_hash: str) -> int:
        return self._register(config_hash, name=name, version=version, config=config)

    def active(self, name: str) -> Row | None:
        return self._decode(
            self.conn.execute(
                f"SELECT * FROM {self.table} WHERE name = ? AND active = 1 ORDER BY id DESC LIMIT 1",
                (name,),
            ).fetchone()
        )


class TimeframeProfileRepository(_HashedRegistry):
    table = "timeframe_profiles"

    def register(self, name: str, version: str, config: dict[str, Any], config_hash: str) -> int:
        return self._register(config_hash, name=name, version=version, config=config)

    def active(self, name: str) -> Row | None:
        return self._decode(
            self.conn.execute(
                f"SELECT * FROM {self.table} WHERE name = ? AND active = 1 ORDER BY id DESC LIMIT 1",
                (name,),
            ).fetchone()
        )


class CostModelRepository(_HashedRegistry):
    table = "cost_models"

    def register(
        self, market: str, asset_class: str, version: str, config: dict[str, Any], config_hash: str
    ) -> int:
        return self._register(
            config_hash, market=market, asset_class=asset_class, version=version, config=config
        )

    def active(self, market: str, asset_class: str) -> Row | None:
        """Cost is keyed on (market, asset_class), never market alone (TRD §6.3)."""
        return self._decode(
            self.conn.execute(
                f"SELECT * FROM {self.table} WHERE market = ? AND asset_class = ? AND active = 1 "
                "ORDER BY id DESC LIMIT 1",
                (market, asset_class),
            ).fetchone()
        )
