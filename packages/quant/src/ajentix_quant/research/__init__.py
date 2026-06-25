"""Strategy-v2 R&D governance (pre-registration, lineage, anti-overfit verification)."""

from .final_verdict import (
    FINAL_VERDICT_SCHEMA_VERSION,
    VERDICT_GO,
    VERDICT_INCONCLUSIVE,
    VERDICT_NO_GO,
    VERDICT_PIVOT_CANDIDATE_CLEARED,
    VerdictInputs,
    build_verdict_inputs,
    decide_final_verdict,
    should_promote_adr_0002,
    summarize_breakeven,
    summarize_pivot,
)
from .preregistration import (
    PLAN_EQUITY_GRID,
    PLAN_FOLDS,
    PLAN_GRID,
    SCHEMA_VERSION,
    VerifyResult,
    build_preregistration,
    load_preregistration,
    verify_preregistration,
)

__all__ = [
    "FINAL_VERDICT_SCHEMA_VERSION",
    "PLAN_EQUITY_GRID",
    "PLAN_FOLDS",
    "PLAN_GRID",
    "SCHEMA_VERSION",
    "VERDICT_GO",
    "VERDICT_INCONCLUSIVE",
    "VERDICT_NO_GO",
    "VERDICT_PIVOT_CANDIDATE_CLEARED",
    "VerdictInputs",
    "VerifyResult",
    "build_preregistration",
    "build_verdict_inputs",
    "decide_final_verdict",
    "load_preregistration",
    "should_promote_adr_0002",
    "summarize_breakeven",
    "summarize_pivot",
    "verify_preregistration",
]
