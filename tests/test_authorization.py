from __future__ import annotations

import copy
import hashlib
import json
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "harness"))

import orchestrator  # noqa: E402
from observability.tracer import OBSERVABILITY_PERMITTED_DATA_V1  # noqa: E402
from workflow import authorization  # noqa: E402


class AuthorizationModuleTests(unittest.TestCase):
    def test_digest_is_deterministic_and_uses_existing_json_serialization(self):
        terms = {"scope": ["workspace", "review"], "label": "café"}
        authorization_data = {**terms, "scope_sha256": "ignored"}
        expected = hashlib.sha256(
            json.dumps(terms, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        ).hexdigest()

        self.assertEqual(authorization.authorization_scope_digest(authorization_data), expected)
        self.assertEqual(authorization.authorization_scope_digest(authorization_data), expected)

    def test_changes_to_authorized_scope_change_the_digest(self):
        original = authorization.new_workflow_authorization(Path("/target"), "digest-test", 2.0)
        changed = copy.deepcopy(original)
        changed["workspace_operations"].append("extra-operation")

        self.assertNotEqual(
            authorization.authorization_scope_digest(original),
            authorization.authorization_scope_digest(changed),
        )

    def test_new_authorization_validates_without_mutating_its_contract(self):
        target = Path("/target")
        created = authorization.new_workflow_authorization(target, "valid-test", 1.25, 3)
        original = copy.deepcopy(created)

        validated = authorization.validate_workflow_authorization(
            {"authorization": created}, target, "valid-test",
        )

        self.assertIs(validated, created)
        self.assertEqual(created, original)
        self.assertEqual(created["scope_sha256"], authorization.authorization_scope_digest(created))
        self.assertIn("relevant_skill_instructions", created["claude_aily"]["permitted_data"])

    def test_modified_digest_and_broadened_scope_are_rejected(self):
        target = Path("/target")
        auth = authorization.new_workflow_authorization(target, "modified-test", 2.0)
        modified_digest = copy.deepcopy(auth)
        modified_digest["claude_aily"]["budget_usd"] = 9.0
        with self.assertRaisesRegex(
            authorization.AuthorizationError, "Saved workflow authorization changed after initialization",
        ):
            authorization.validate_workflow_authorization(
                {"authorization": modified_digest}, target, "modified-test",
            )

        broadened_scope = copy.deepcopy(auth)
        broadened_scope["workspace_operations"].append("access-outside-target")
        broadened_scope["scope_sha256"] = authorization.authorization_scope_digest(broadened_scope)
        with self.assertRaisesRegex(
            authorization.AuthorizationError, "Saved workspace authorization is missing, changed, or broader",
        ):
            authorization.validate_workflow_authorization(
                {"authorization": broadened_scope}, target, "modified-test",
            )

    def test_missing_and_unsupported_legacy_authorization_are_rejected(self):
        with self.assertRaisesRegex(
            authorization.AuthorizationError, "no supported persistent authorization",
        ):
            authorization.validate_workflow_authorization({}, Path("/target"), "missing")

        unsupported = {"authorization": {"version": 0}}
        with self.assertRaisesRegex(
            authorization.AuthorizationError, "no supported persistent authorization",
        ):
            authorization.validate_workflow_authorization(unsupported, Path("/target"), "legacy")

    def test_legacy_single_review_authorization_without_observability_remains_valid(self):
        target = Path("/target")
        legacy = authorization.new_workflow_authorization(
            target, "legacy-test", 2.0, max_code_review_calls=1,
        )
        legacy["claude_aily"].pop("code_review_policy")
        legacy.pop("observability")
        legacy["scope_sha256"] = authorization.authorization_scope_digest(legacy)

        validated = authorization.validate_workflow_authorization(
            {"authorization": legacy}, target, "legacy-test",
        )

        self.assertIs(validated, legacy)
        self.assertNotIn("observability", validated)
        self.assertNotIn("code_review_policy", validated["claude_aily"])

    def test_legacy_claude_scope_remains_valid_without_authorizing_skill_bodies(self):
        target = Path("/target")
        legacy = authorization.new_workflow_authorization(target, "legacy-skill-scope", 2.0)
        legacy["claude_aily"]["permitted_data"] = authorization.LEGACY_CLAUDE_DATA_SCOPE.copy()
        legacy["scope_sha256"] = authorization.authorization_scope_digest(legacy)

        validated = authorization.validate_workflow_authorization(
            {"authorization": legacy}, target, "legacy-skill-scope",
        )

        self.assertEqual(validated["claude_aily"]["permitted_data"], authorization.LEGACY_CLAUDE_DATA_SCOPE)
        self.assertNotIn("relevant_skill_instructions", validated["claude_aily"]["permitted_data"])

    def test_legacy_observability_scope_version_remains_valid(self):
        target = Path("/target")
        legacy = authorization.new_workflow_authorization(
            target, "legacy-tracing-test", 2.0, tracing_enabled=True,
        )
        legacy["observability"]["version"] = 1
        legacy["observability"]["permitted_data"] = OBSERVABILITY_PERMITTED_DATA_V1.copy()
        legacy["scope_sha256"] = authorization.authorization_scope_digest(legacy)

        validated = authorization.validate_workflow_authorization(
            {"authorization": legacy}, target, "legacy-tracing-test",
        )

        self.assertEqual(validated["observability"]["version"], 1)

    def test_orchestrator_compatibility_wrappers_translate_authorization_errors(self):
        with self.assertRaisesRegex(
            orchestrator.WorkflowError, "Code review call authorization must be between 1 and 64",
        ):
            orchestrator.new_workflow_authorization(Path("/target"), "compat-test", 1.0, 0)

        with self.assertRaisesRegex(
            orchestrator.WorkflowError, "no supported persistent authorization",
        ):
            orchestrator.validate_workflow_authorization({}, Path("/target"), "compat-test")


if __name__ == "__main__":
    unittest.main()
