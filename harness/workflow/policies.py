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
    read_only: bool = False,
) -> PolicyDecision:
    if proposal_policy not in {"required", "optional", "disabled", "automatic"}:
        raise ValueError("Unsupported proposal policy.")
    if approval_policy not in {"always", "never", "auto"}:
        raise ValueError("Unsupported approval policy.")
    if constraints.proposal_policy == "required" and proposal_policy != "required":
        raise ValueError("Repository policy requires a proposal.")
    if constraints.approval_policy == "always" and approval_policy != "always":
        raise ValueError("Repository policy requires explicit human approval.")
    policy_requires_proposal = proposal_policy == "required" or (
        proposal_policy == "automatic" and assessment.proposal_recommended
    )
    assessment_requires_proposal = not read_only and (
        assessment.risk == "high" or assessment.complexity == "high"
        or assessment.security_impact or assessment.production_impact or assessment.architectural_impact
    )
    proposal_required = policy_requires_proposal or assessment_requires_proposal or (
        constraints.proposal_policy == "required"
    )
    risk_requires_approval = (
        not read_only and (assessment.risk == "high" or assessment.security_impact
                           or assessment.production_impact or assessment.ambiguous)
    )
    approval_required = not read_only and (
        explicit_approval_required or approval_policy == "always"
        or (approval_policy == "auto" and assessment.approval_recommended)
        or risk_requires_approval or constraints.approval_policy == "always"
    )
    reason = "Repository policy gates remain authoritative."
    if policy_requires_proposal or assessment_requires_proposal:
        reason += " Task assessment requires proposal and planning gates."
    if risk_requires_approval:
        reason += " Task risk requires explicit human approval."
    if explicit_approval_required:
        reason += " The task explicitly requires human approval."
    return PolicyDecision(proposal_required, approval_required, reason)
