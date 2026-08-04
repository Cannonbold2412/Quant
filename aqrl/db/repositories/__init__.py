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
from .embeddings import EmbeddingRepository
from .jobs import JOB_STATUSES, JOB_TYPES, JobRepository, LeaseLost, UnknownJobType
from .knowledge import (
    KnowledgeEdgeRepository,
    KnowledgeEntryRepository,
    LabNotebookRepository,
    repeat_failure_rate,
)
from .operators import (
    DuplicateSpecError,
    OperatorRepository,
    SpecOperatorRepository,
    SpecRepository,
)
from .promotion import PromotionRepository
from .registry import CostModelRepository, MarketProfileRepository, TimeframeProfileRepository
from .research import (
    VERDICT_TO_STATUS,
    CodeVersionRepository,
    EvaluationRepository,
    EvaluationTestRepository,
    ExperimentRepository,
    NullWorldRunRepository,
    RegimePerformanceRepository,
    ResearchGoalRepository,
    ResearchPlanRepository,
    ResearchQuestionRepository,
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
    "EmbeddingRepository",
    "EvaluationRepository",
    "EvaluationTestRepository",
    "ExperimentRepository",
    "IndexMembershipRepository",
    "JobRepository",
    "KnowledgeEdgeRepository",
    "KnowledgeEntryRepository",
    "LabNotebookRepository",
    "LeaseLost",
    "MarketProfileRepository",
    "NullWorldRunRepository",
    "OperatorRepository",
    "PromotionRepository",
    "RegimePerformanceRepository",
    "Repository",
    "ResearchGoalRepository",
    "ResearchPlanRepository",
    "ResearchQuestionRepository",
    "Row",
    "SnapshotRepository",
    "SpecOperatorRepository",
    "SpecRepository",
    "StrategyRepository",
    "TimeframeProfileRepository",
    "UnknownJobType",
    "ValidationFlagRepository",
    "new_uid",
    "repeat_failure_rate",
    "utcnow_iso",
]
