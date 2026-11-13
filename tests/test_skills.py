from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "harness"))

from workflow import skills, workspace  # noqa: E402
from workflow.authorization import LEGACY_CLAUDE_DATA_SCOPE  # noqa: E402
import orchestrator  # noqa: E402


class SkillResolutionTests(unittest.TestCase):
    def test_loads_bundled_skills_and_referenced_architecture_guides(self):
        with tempfile.TemporaryDirectory() as root:
            discovered = workspace.project_skills(Path(root), "Refactor workflow module boundaries")

        by_name = {skill["name"]: skill for skill in discovered}
        self.assertTrue({
            "architecture-design", "software-engineering", "testing-strategy", "code-review",
        }.issubset(by_name))
        architecture = by_name["architecture-design"]
        self.assertEqual(architecture["source"], "harness")
        self.assertIn("## Changes to existing systems", architecture["content"])
        self.assertIn("# Architecture approaches", architecture["content"])
        self.assertIn("# Scenario examples", architecture["content"])
        for name in ("software-engineering", "testing-strategy", "code-review"):
            skill_root = workspace.HARNESS_SKILLS_ROOT / name
            skill_text = (skill_root / "SKILL.md").read_text(encoding="utf-8")
            self.assertEqual(workspace.skill_metadata(skill_root / "SKILL.md", skill_text)[0], name)
            self.assertTrue((skill_root / "agents" / "openai.yaml").is_file())

    def test_target_skill_overrides_bundled_skill_with_same_name(self):
        with tempfile.TemporaryDirectory() as root:
            skill_path = Path(root) / ".agents" / "skills" / "software-engineering" / "SKILL.md"
            skill_path.parent.mkdir(parents=True)
            skill_path.write_text(
                "---\nname: software-engineering\ndescription: Target convention.\n---\n"
                "Target-specific engineering rule.\n",
                encoding="utf-8",
            )
            codex_skill_path = Path(root) / ".codex" / "skills" / "project-conventions" / "SKILL.md"
            codex_skill_path.parent.mkdir(parents=True)
            codex_skill_path.write_text(
                "---\nname: project-conventions\ndescription: Repository conventions.\n---\n"
                "Use explicit service names.\n",
                encoding="utf-8",
            )
            discovered = workspace.project_skills(Path(root), "Implement a change")

        matching = [skill for skill in discovered if skill["name"] == "software-engineering"]
        self.assertEqual(len(matching), 1)
        self.assertEqual(matching[0]["source"], "project")
        self.assertIn("Target-specific engineering rule.", matching[0]["content"])
        self.assertTrue(any(skill["name"] == "project-conventions" for skill in discovered))

    def test_stage_selection_is_provider_independent_and_skips_irrelevant_architecture(self):
        guidance = {
            "skills": [
                {"name": name, "content": f"instructions for {name}"}
                for name in (
                    "architecture-design", "software-engineering", "testing-strategy", "code-review",
                )
            ],
        }

        planning = skills.select_skills(guidance, "planning", "Fix a typo in README")
        code_review = skills.select_skills(guidance, "code_review", "Review changed implementation modules")
        correction = skills.select_skills(guidance, "correction", "Fix a module boundary regression")

        self.assertEqual(planning["skills"], [])
        self.assertEqual(
            [item["name"] for item in code_review["skills"]],
            ["code-review", "software-engineering", "testing-strategy"],
        )
        self.assertEqual(
            [item["name"] for item in correction["skills"]],
            ["code-review", "software-engineering", "testing-strategy", "architecture-design"],
        )

    def test_legacy_guidance_without_skill_entries_remains_usable(self):
        selected = skills.select_skills({"files": ["README.md"]}, "implementation", "Add a feature")
        self.assertEqual(selected["files"], ["README.md"])
        self.assertEqual(selected["skills"], [])

    def test_legacy_authorization_does_not_expand_claude_skill_content_scope(self):
        runner = object.__new__(orchestrator.Orchestrator)
        runner.state = {"authorization": {"claude_aily": {"permitted_data": LEGACY_CLAUDE_DATA_SCOPE}}}
        guidance = {
            "skills": [{
                "name": "software-engineering", "source": "harness", "path": "skill/SKILL.md",
                "description": "Practical engineering guidance.", "content": "Private full skill body.",
            }],
        }

        prompt_section = orchestrator._guidance_for_claude(
            guidance,
            stage="plan_review",
            context="Implement software code",
            include_skill_content=runner._claude_skill_content_authorized(),
        )

        self.assertFalse(runner._claude_skill_content_authorized())
        self.assertIn("Practical engineering guidance.", prompt_section)
        self.assertNotIn("Private full skill body.", prompt_section)


if __name__ == "__main__":
    unittest.main()
