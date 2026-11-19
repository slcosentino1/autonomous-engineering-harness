"""Proposal and approval policy resolution against trusted repository gates."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from workflow.assessment import Assessment
from workflow.definitions import RepositoryWorkflowConstraints


class ProposalPolicy(StrEnum):
    REQUIRED = "required"
    OPTIONAL = "optional"
    DISABLED = "disabled"
    AUTOMATIC = "automatic"


class ApprovalPolicy(StrEnum):
    ALWAYS = "always"
    NEVER = "never"
    AUTO = "auto"


@dataclass(frozen=True)
class PolicyDecision:
    proposal_required: bool
    approval_required: bool
    reason: str


def resolve_policies(
    *,
    proposal_policy: str,
    approval_policy: str,
    assessment: Assessment,
    constraints: RepositoryWorkflowConstraints,
    explicit_approval_required: bool = False,
) -> PolicyDecision:
    if proposal_policy not in {"required", "optional", "disabled", "automatic"}:
        raise ValueError("Unsupported proposal policy.")
    if approval_policy not in {"always", "never", "auto"}:
        raise ValueError("Unsupported approval policy.")
    if constraints.proposal_policy == "required" and proposal_policy != "required":
        raise ValueError("Repository policy requires a proposal.")
    if constraints.approval_policy == "always" and approval_policy != "always":
        raise ValueError("Repository policy requires explicit human approval.")
    proposal_required = proposal_policy == "required" or (
        proposal_policy == "automatic" and assessment.proposal_recommended
    )
    approval_required = explicit_approval_required or approval_policy == "always" or (
        approval_policy == "auto" and assessment.approval_recommended
    )
    reason = "Repository policy gates remain authoritative."
    if explicit_approval_required:
        reason += " The task explicitly requires human approval."
    return PolicyDecision(proposal_required, approval_required, reason)
