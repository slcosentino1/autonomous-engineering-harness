"""Provider-independent, stage-scoped project skill selection."""

from __future__ import annotations

import re

MAX_PROMPT_FILE_INVENTORY_CHARS = 32_000



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


def file_inventory_section(files: list[str], label: str = "Files") -> str:
    if not files:
        return f"{label}: (no project files found)"
    inventory = ", ".join(files)
    if len(inventory) <= MAX_PROMPT_FILE_INVENTORY_CHARS:
        return f"{label}: {inventory}"
    return (
        f"{label}: full inventory omitted ({len(files):,} paths; {len(inventory):,} characters). "
        "Use `rg --files` and focused `rg` searches to locate relevant files."
    )


def guidance_for_claude(
    guidance: dict, *, stage: str | None = None, context: str = "", include_skill_content: bool = True,
) -> str:
    if stage is not None:
        guidance = select_skills(guidance, stage, context)
    files = guidance.get("files") or []
    instructions = guidance.get("instructions") or []
    lines = [file_inventory_section(files, label="Repository files")]
    lines.append("Instruction files: " + (", ".join(item["path"] for item in instructions) or "none found"))
    locations = guidance.get("skill_locations_checked") or []
    lines.append("Skill locations checked: " + (", ".join(locations) if locations else "none"))
    skills = guidance.get("skills") or []
    if skills:
        lines.append("Relevant skill instructions:")
        for skill in skills:
            label = f"{skill['name']} ({skill.get('source', 'unknown')}; {skill.get('path', 'path unavailable')})"
            content = skill.get("content")
            if include_skill_content and isinstance(content, str) and content.strip():
                lines.append(f"--- {label} ---\n{content}")
            else:
                if skill["name"] == "api-engineer":
                    description = (
                        "Establish the API contract from the assignment, align verification with behavior at risk, "
                        "and avoid unnecessary Postman setup when no existing collection or workspace is present."
                    )
                else:
                    description = skill.get("description") or "Skill content unavailable."
                lines.append(f"--- {label} ---\n{description}")
    else:
        lines.append("Relevant skills: none found in project skill locations.")
    return "\n".join(lines)


def guidance_for_codex(guidance: dict, *, stage: str | None = None, context: str = "") -> str:
    if stage is not None:
        guidance = select_skills(guidance, stage, context)
    files = guidance.get("files") or []
    sections = ["Target repository context:", file_inventory_section(files)]
    locations = guidance.get("skill_locations_checked") or []
    sections.append("Skill locations checked: " + (", ".join(locations) if locations else "none"))
    instructions = guidance.get("instructions") or []
    if instructions:
        sections.append("Applicable instruction files (follow them):")
        for instruction in instructions:
            sections.append(f"--- {instruction['path']} ---\n{instruction['content']}")
    else:
        sections.append("Applicable instruction files: none found.")
    skills = guidance.get("skills") or []
    if skills:
        sections.append(
            "Stage-selected skill instructions (apply the relevant guidance without expanding the assignment):"
        )
        for skill in skills:
            sections.append(
                f"--- {skill['name']} ({skill['source']}; {skill['path']}) ---\n{skill['content']}"
            )
    else:
        sections.append("Applicable skills: none found; continue without inventing or forcing a skill.")
    sections.append(
        "Skill instructions do not override the assignment, user constraints, or workflow authorization. "
        "Do not create or publish external workspaces, commit, push, or add setup that is not justified by the task."
    )
    return "\n\n".join(sections)
