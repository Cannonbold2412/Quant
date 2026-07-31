"""Repositories.

Hand-written repositories exist for the tables Stage 0 and Stage 1 actually
exercise. Everything else in the schema is reachable through the generic
`Repository` base until a felt need arrives — Backend-Schema §1's
build-on-need principle applied to code rather than to DDL. Stage 4
(Implementation_Plan §6) is the first felt need for `jobs` and `audit_log`,
hence `JobRepository` and `AuditLogRepository` below.
"""

from .audit import AuditLogRepository
from .base import Repository, Row, new_uid, utcnow_iso
from .data import (
    CorporateActionRepository,
    IndexMembershipRepository,
    SnapshotRepository,
    ValidationFlagRepository,
)
from .jobs import JOB_STATUSES, JOB_TYPES, JobRepository, LeaseLost, UnknownJobType
from .knowledge import KnowledgeEntryRepository
from .operators import (
    DuplicateSpecError,
    OperatorRepository,
    SpecOperatorRepository,
    SpecRepository,
)
from .registry import CostModelRepository, MarketProfileRepository, TimeframeProfileRepository
from .research import (
    VERDICT_TO_STATUS,
    CodeVersionRepository,
    EvaluationRepository,
    EvaluationTestRepository,
    ExperimentRepository,
    NullWorldRunRepository,
    RegimePerformanceRepository,
    ResearchPlanRepository,
    StrategyRepository,
)

__all__ = [
    "JOB_STATUSES",
    "JOB_TYPES",
    "VERDICT_TO_STATUS",
    "AuditLogRepository",
    "CodeVersionRepository",
    "CorporateActionRepository",
    "CostModelRepository",
    "DuplicateSpecError",
    "EvaluationRepository",
    "EvaluationTestRepository",
    "ExperimentRepository",
    "IndexMembershipRepository",
    "JobRepository",
    "KnowledgeEntryRepository",
    "LeaseLost",
    "MarketProfileRepository",
    "NullWorldRunRepository",
    "OperatorRepository",
    "RegimePerformanceRepository",
    "Repository",
    "ResearchPlanRepository",
    "Row",
    "SnapshotRepository",
    "SpecOperatorRepository",
    "SpecRepository",
    "StrategyRepository",
    "TimeframeProfileRepository",
    "UnknownJobType",
    "ValidationFlagRepository",
    "new_uid",
    "utcnow_iso",
]
