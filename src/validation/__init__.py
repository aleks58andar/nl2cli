"""Deciding whether a Plan may run, and whether it needs confirmation.

Depends only on `core`. Nothing here executes anything, and nothing here
calls the LLM — a plan is judged purely against local rules and host facts.
"""

from src.validation.host_compat import (
    enhance_plan_with_context,
    validate_plan_against_host,
)
from src.validation.risk import (
    compute_risk_score,
    needs_confirmation,
    one_line_summary,
)
from src.validation.safety import SafetyChecker, SafetyError, get_safety_checker
from src.validation.validators import (
    enhance_and_validate_plan,
    format_validation_issues,
    has_critical_issues,
    validate_plan,
)

__all__ = [
    "SafetyChecker",
    "SafetyError",
    "compute_risk_score",
    "enhance_plan_with_context",
    "enhance_and_validate_plan",
    "format_validation_issues",
    "get_safety_checker",
    "has_critical_issues",
    "needs_confirmation",
    "one_line_summary",
    "validate_plan",
    "validate_plan_against_host",
]
