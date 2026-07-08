"""Dashboard governance policy, resolution and enforcement helpers.

The package is intentionally independent from dashboard authentication: auth
providers verify who the caller is; this package decides what that caller may do.
"""

from .loader import GovernancePolicyError, load_governance_policy, parse_governance_policy, save_governance_policy
from .models import AccessDecision, EffectiveAccess, GovernancePolicy, GovernanceSubject
from .resolver import resolve_effective_access

__all__ = [
    "AccessDecision",
    "EffectiveAccess",
    "GovernancePolicy",
    "GovernancePolicyError",
    "GovernanceSubject",
    "load_governance_policy",
    "parse_governance_policy",
    "resolve_effective_access",
    "save_governance_policy",
]
