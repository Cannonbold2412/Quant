"""Repositories for the operator library and strategy specs.

Two jobs, both about spending compute only once:

* `OperatorRepository.sync` mirrors the in-code registry into the `operators`
  table so `spec_operators` has something to join against — and so a human can
  see what the library contains without reading Python.
* `SpecRepository.insert_spec` **rejects an exact duplicate before any compute
  is spent** (App-Flow §3.4) and records which operators a spec used, so
  *"which experiments ever used a Kalman filter?"* is an indexed join rather
  than a scan over JSON (Backend-Schema §15 Q4).

The duplicate rejection is not a convenience. `strategy_specs.spec_hash` is
UNIQUE, so the database would refuse the insert anyway — but it would do so with
an opaque `IntegrityError` after the caller had already decided to run an
experiment. Catching it here, with the prior spec attached, is what lets A1 be
told *"you already tried this, and here is what happened"*.
"""
from __future__ import annotations

from typing import Any

from ...operators import StrategySpec, all_operators, operator_library_version
from .base import Repository, Row

__all__ = [
    "DuplicateSpecError",
    "OperatorRepository",
    "SpecOperatorRepository",
    "SpecRepository",
]


class DuplicateSpecError(ValueError):
    """This exact operator composition has been tried before.

    Carries `spec_hash` and the prior row so the caller can attach the earlier
    result rather than merely refusing.
    """

    def __init__(self, spec_hash: str, existing: Row) -> None:
        self.spec_hash = spec_hash
        self.existing = existing
        super().__init__(
            f"spec_hash {spec_hash[:16]}... already exists as strategy_specs.id={existing['id']} "
            f"(strategy_id={existing['strategy_id']}, version={existing['version']}). "
            "An exact re-run is rejected before compute is spent (App-Flow §3.4)."
        )


class OperatorRepository(Repository):
    table = "operators"
    json_columns = frozenset({"parameters", "valid_markets", "valid_timeframes"})

    def by_name(self, name: str, version: str) -> Row | None:
        return self._decode(
            self.conn.execute(
                "SELECT * FROM operators WHERE name = ? AND version = ?", (name, version)
            ).fetchone()
        )

    def sync(self, approved_by: str | None = None) -> dict[str, int]:
        """Mirror the in-code registry into the table. Idempotent.

        `approved_by` stays NULL unless a human passes their name: *"the library
        is not self-modifying"* (TRD §11.1). Registering an operator in code
        makes it available to the compiler; approving it is a separate,
        deliberate act by a person.

        `test_status` is `tested` because the registry-wide causality and
        contract suites cover every registered operator by construction — an
        untested operator cannot exist without the suite failing.
        """
        counts = {"inserted": 0, "updated": 0, "unchanged": 0}
        for operator in all_operators():
            descriptor = operator.descriptor()
            fields: dict[str, Any] = {
                "category": operator.category,
                "implementation_ref": descriptor["implementation_ref"],
                "parameters": descriptor["params"],
                "valid_markets": descriptor["valid_markets"],
                "valid_timeframes": descriptor["valid_timeframes"],
                "description": operator.description,
                "reference_notes": operator.references,
                "test_status": "tested",
            }
            existing = self.by_name(operator.name, operator.version)
            if existing is None:
                self.insert(name=operator.name, version=operator.version, **fields)
                counts["inserted"] += 1
                continue
            if any(existing.get(key) != value for key, value in fields.items()):
                self.update(existing["id"], **fields)
                counts["updated"] += 1
            else:
                counts["unchanged"] += 1
            if approved_by and not existing.get("approved_by"):
                self.update(existing["id"], approved_by=approved_by)
        return counts

    def approve(self, name: str, version: str | None, approved_by: str) -> int:
        """Record a human's sign-off. The only way `approved_by` is ever set."""
        if not approved_by:
            raise ValueError("approved_by is required: the library is not self-modifying")
        if version is None:
            rows = self.find(name=name, order_by="version")
            if not rows:
                raise LookupError(f"no operator named {name!r} in the table; run `aqrl operators sync`")
            targets = [rows[-1]]
        else:
            row = self.by_name(name, version)
            if row is None:
                raise LookupError(f"no operator {name}@{version} in the table")
            targets = [row]
        for row in targets:
            self.update(row["id"], approved_by=approved_by)
        return len(targets)

    def library_version(self) -> str:
        """The value stamped on an experiment (TRD §6.6). Derived from code."""
        return operator_library_version()


class SpecOperatorRepository(Repository):
    """The `spec_operators` join — which operators a spec used, in which role.

    A declared class rather than a generic `Repository` with its attributes
    reassigned per call: this table has neither `uid` nor `created_at`, so the
    base class's defaults must be switched off, and doing that at four call
    sites is one forgotten line away from an insert against a column that does
    not exist.
    """

    table = "spec_operators"
    json_columns = frozenset({"parameters_used"})
    has_uid = False
    created_column = None

    def for_spec(self, spec_id: int) -> list[Row]:
        return self.find(spec_id=spec_id, order_by="id")


class SpecRepository(Repository):
    table = "strategy_specs"
    json_columns = frozenset(
        {
            "entry_logic",
            "exit_logic",
            "filter_logic",
            "risk_logic",
            "universe",
            "parameters",
            "source_external_knowledge_ids",
            "source_internal_knowledge_ids",
        }
    )

    def by_hash(self, spec_hash: str) -> Row | None:
        return self._decode(
            self.conn.execute(
                "SELECT * FROM strategy_specs WHERE spec_hash = ?", (spec_hash,)
            ).fetchone()
        )

    def load_spec(self, spec_id: int) -> StrategySpec:
        """Reconstitute the `StrategySpec` a stored `strategy_specs` row encodes.

        The inverse of `insert_spec`'s flattening. Stage 4's `EVALUATE` handler
        (`aqrl/orchestration/handlers/evaluate.py`) is the first caller that
        needs a spec back out of the database rather than off disk — a queued
        job carries `spec_id`, not the object itself.
        """
        row = self.get(spec_id)
        if row is None:
            raise KeyError(f"no strategy_specs row {spec_id}")
        return StrategySpec(
            entry_logic=row["entry_logic"] or [],
            exit_logic=row["exit_logic"] or [],
            filter_logic=row["filter_logic"] or [],
            risk_logic=row["risk_logic"] or [],
            universe=row["universe"] or {},
            parameters=row["parameters"] or {},
            hypothesis=row["hypothesis"] or "",
            rationale=row["rationale"],
            expected_behavior=row["expected_behavior"],
        )

    def next_version(self, strategy_id: int) -> int:
        row = self.conn.execute(
            "SELECT COALESCE(MAX(version), 0) AS latest FROM strategy_specs WHERE strategy_id = ?",
            (strategy_id,),
        ).fetchone()
        return int(row["latest"]) + 1

    def insert_spec(
        self,
        spec: StrategySpec,
        strategy_id: int,
        version: int | None = None,
        **fields: Any,
    ) -> int:
        """Validate, reject duplicates, insert, and record operator usage.

        Validation runs first: a spec that cannot resolve against the registry
        should fail here, where it costs nothing, rather than inside a worker
        that has already claimed a job.
        """
        spec.validate_spec()
        digest = spec.spec_hash()

        existing = self.by_hash(digest)
        if existing is not None:
            raise DuplicateSpecError(digest, existing)

        spec_id = self.insert(
            strategy_id=strategy_id,
            version=version if version is not None else self.next_version(strategy_id),
            hypothesis=spec.hypothesis,
            rationale=spec.rationale,
            entry_logic=[node.model_dump(mode="json") for node in spec.entry_logic],
            exit_logic=[node.model_dump(mode="json") for node in spec.exit_logic],
            filter_logic=[node.model_dump(mode="json") for node in spec.filter_logic],
            risk_logic=[node.model_dump(mode="json") for node in spec.risk_logic],
            universe=spec.universe,
            parameters=spec.parameters,
            spec_hash=digest,
            expected_behavior=spec.expected_behavior,
            **fields,
        )
        self._record_operators(spec, spec_id)
        return spec_id

    def _record_operators(self, spec: StrategySpec, spec_id: int) -> None:
        """Populate `spec_operators` — one row per (operator, role) used.

        Transitive: a spec whose entry root is a crossover fed by two rolling
        means used all three, and a query for experiments using `rolling_mean`
        must find it.
        """
        operators = OperatorRepository(self.conn)
        join = SpecOperatorRepository(self.conn)

        for role, entries in spec.operators().items():
            for _, operator, bound in entries:
                row = operators.by_name(operator.name, operator.version)
                if row is None:
                    # The table lags the registry; sync on demand rather than
                    # dropping the provenance link.
                    operators.sync()
                    row = operators.by_name(operator.name, operator.version)
                if row is None:  # pragma: no cover - sync just inserted it
                    continue
                join.insert(
                    spec_id=spec_id,
                    operator_id=row["id"],
                    role=role,
                    parameters_used=bound,
                )

    def using_operator(self, name: str) -> list[Row]:
        """Backend-Schema §15 Q4 — indexed join, never a scan over JSON."""
        rows = self.conn.execute(
            """SELECT DISTINCT s.* FROM strategy_specs s
                 JOIN spec_operators so ON so.spec_id = s.id
                 JOIN operators o       ON o.id = so.operator_id
                WHERE o.name = ?
                ORDER BY s.id""",
            (name,),
        ).fetchall()
        return [self._decode(row) for row in rows]  # type: ignore[misc]
