"""Provider-independent, stage-scoped project skill selection."""

from __future__ import annotations

import re


STAGE_SKILLS = {
    "planning": ("architecture-design", "software-engineering", "testing-strategy"),
    "plan_review": ("architecture-design", "software-engineering", "testing-strategy"),
    "refinement": ("architecture-design", "software-engineering", "testing-strategy"),
    "human_revision": ("architecture-design", "software-engineering", "testing-strategy"),
    "implementation": ("software-engineering", "testing-strategy", "architecture-design"),
    "code_review": ("code-review", "software-engineering", "testing-strategy"),
    "correction": (
        "code-review", "software-engineering", "testing-strategy", "architecture-design",
    ),
}

_ARCHITECTURE_TERMS = (
    "architect", "modul", "responsibilit", "boundar", "dependenc", "orchestrat", "refactor",
    "layer", "integrat", "scale", "design", "service", "component", "workflow", "api", "rest",
)
_SOFTWARE_ENGINEERING_TERMS = (
    "code", "software", "implement", "implementation", "feature", "function", "class", "module",
    "api", "rest", "endpoint", "bug", "refactor", "workflow", "service", "database",
    "behavior", "logic", "parser", "component", "helper",
)
_TESTING_TERMS = (
    "test", "testing", "validation", "verify", "regression", "behavior", "implementation", "code",
    "feature", "bug", "refactor", "workflow", "recovery", "state", "parser", "api", "service",
    "function", "logic", "helper", "correctness",
)
_DESCRIPTION_STOP_WORDS = frozenset({
    "and", "are", "for", "from", "into", "when", "where", "with", "this", "that", "the", "use",
    "used", "using", "help", "apply", "applying", "across", "about", "especially",
})


def stage_key(stage: str) -> str:
    if stage.startswith("plan_review_"):
        return "plan_review"
    if stage.startswith("plan_refine_"):
        return "refinement"
    return stage


def select_skills(guidance: dict, stage: str, context: str = "") -> dict:
    """Return the saved guidance with only skills intended for this stage."""
    key = stage_key(stage)
    names = STAGE_SKILLS.get(key, ())
    if not names:
        return {**guidance, "skills": []}
    available = {}
    for skill in guidance.get("skills") or []:
        name = skill.get("name")
        if isinstance(name, str) and name not in available:
            available[name] = skill

    normalized_context = context.casefold()
    context_terms = set(re.findall(r"[a-z][a-z0-9-]*", normalized_context))
    selected = []
    for name in names:
        skill = available.get(name)
        if skill is None:
            continue
        terms = {
            "architecture-design": _ARCHITECTURE_TERMS,
            "software-engineering": _SOFTWARE_ENGINEERING_TERMS,
            "testing-strategy": _TESTING_TERMS,
        }.get(name)
        always_for_stage = key in {"code_review", "correction"} and name in {
            "software-engineering", "testing-strategy",
        }
        if terms is not None and not always_for_stage and not any(
            token == term or token.startswith(term) for token in context_terms for term in terms
        ):
            continue
        selected.append(skill)

    selected_names = {skill["name"] for skill in selected}
    for skill in guidance.get("skills") or []:
        name = skill.get("name")
        if not isinstance(name, str) or name in selected_names:
            continue
        if name in {"architecture-design", "software-engineering", "testing-strategy", "code-review"}:
            continue
        if name == "api-engineer" and any(
            term in normalized_context for term in ("api", "rest", "endpoint")
        ):
            selected.append(skill)
            selected_names.add(name)
            continue
        description_terms = {
            term for term in re.findall(r"[a-z][a-z0-9-]{2,}", str(skill.get("description") or "").casefold())
            if term not in _DESCRIPTION_STOP_WORDS
        }
        if description_terms & context_terms:
            selected.append(skill)
            selected_names.add(name)
    return {**guidance, "skills": selected}
