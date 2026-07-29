"""Repositories.

Hand-written repositories exist for the tables Stage 0 and Stage 1 actually
exercise. Everything else in the schema is reachable through the generic
`Repository` base until a felt need arrives — Backend-Schema §1's
build-on-need principle applied to code rather than to DDL:

    from aqrl.db.repositories import Repository
    class JobRepository(Repository):
        table = "jobs"
"""

from .base import Repository, Row, new_uid, utcnow_iso
from .data import (
    CorporateActionRepository,
    IndexMembershipRepository,
    SnapshotRepository,
    ValidationFlagRepository,
)
from .operators import DuplicateSpecError, OperatorRepository, SpecRepository
from .registry import CostModelRepository, MarketProfileRepository, TimeframeProfileRepository
from .research import (
    VERDICT_TO_STATUS,
    EvaluationRepository,
    ExperimentRepository,
    NullWorldRunRepository,
    StrategyRepository,
)

__all__ = [
    "VERDICT_TO_STATUS",
    "CorporateActionRepository",
    "CostModelRepository",
    "DuplicateSpecError",
    "EvaluationRepository",
    "ExperimentRepository",
    "IndexMembershipRepository",
    "MarketProfileRepository",
    "NullWorldRunRepository",
    "OperatorRepository",
    "Repository",
    "Row",
    "SnapshotRepository",
    "SpecRepository",
    "StrategyRepository",
    "TimeframeProfileRepository",
    "ValidationFlagRepository",
    "new_uid",
    "utcnow_iso",
]
