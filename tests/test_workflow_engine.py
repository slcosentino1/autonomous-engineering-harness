from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "harness"))

from workflow.assessment import assessment_from_dict, assess_task, reassess_task
from workflow.definitions import (
    BUILT_IN_WORKFLOWS,
    FULL_DEVELOPMENT,
    RepositoryWorkflowConstraints,
    resolve_workflow,
)
from workflow.engine import (
    StageCondition,
    StageDefinition,
    StageDependency,
    StageRegistry,
    StageResult,
    StageStatus,
    WorkflowDefinition,
    WorkflowDefinitionError,
    WorkflowEngine,
    WorkflowContext,
    definition_snapshot,
)
from workflow.policies import resolve_policies


class RecordingStage:
    def __init__(self, name: str, order: list[str], result: StageResult | None = None):
        self.name = name
        self.order = order
        self.result = result or StageResult(output={"stage": name})

    def execute(self, stage_input):
        self.order.append(self.name)
        return self.result


class WorkflowEngineTests(unittest.TestCase):
    def test_registered_stages_execute_in_dependency_order_and_persist_running(self):
        order: list[str] = []
        registry = StageRegistry()
        registry.register("first", RecordingStage("first", order))
        registry.register("second", RecordingStage("second", order))
        definition = WorkflowDefinition(
            "custom", "test flow", 1,
            (StageDefinition("a", "first"), StageDefinition("b", "second", (StageDependency("a"),))),
        )
        saves: list[list[str]] = []
        engine = WorkflowEngine(
            registry,
            lambda current: saves.append([record["status"] for record in current["stages"].values()]),
        )
        state = engine.execute(definition, WorkflowContext("wf", "task"))
        self.assertEqual(order, ["first", "second"])
        self.assertEqual(state["status"], "complete")
        self.assertEqual(state["stages"]["a"]["status"], "completed")
        self.assertIn(["running"], saves)

    def test_full_development_definition_completes_with_conditional_correction_not_applicable(self):
        order: list[str] = []
        registry = StageRegistry()
        for implementation in {stage.implementation for stage in FULL_DEVELOPMENT.stages}:
            output = {"has_findings": False} if implementation == "code_review" else {"stage": implementation}
            registry.register(implementation, RecordingStage(implementation, order, StageResult(output=output)))
        result = WorkflowEngine(registry, lambda _state: None).execute(FULL_DEVELOPMENT, WorkflowContext("wf", "task"))
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["stages"]["correction"]["status"], "not_applicable")
        self.assertNotIn("correction", order)
        self.assertEqual(result["stages"]["complete"]["status"], "completed")

    def test_registry_and_definition_validation_reject_duplicates_unknowns_and_bad_dependencies(self):
        registry = StageRegistry()
        stage = RecordingStage("work", [])
        registry.register("work", stage)
        with self.assertRaisesRegex(WorkflowDefinitionError, "already registered"):
            registry.register("work", stage)
        missing = WorkflowDefinition("custom", "test", 1, (StageDefinition("a", "missing"),))
        with self.assertRaisesRegex(WorkflowDefinitionError, "No implementation"):
            WorkflowEngine(registry, lambda _state: None).execute(missing, WorkflowContext("wf", "task"))
        bad_dependency = WorkflowDefinition(
            "custom", "test", 1,
            (StageDefinition("a", "work", (StageDependency("later"),)),),
        )
        with self.assertRaisesRegex(WorkflowDefinitionError, "unknown or later"):
            WorkflowEngine(registry, lambda _state: None).execute(bad_dependency, WorkflowContext("wf", "task"))
        duplicate_stage = WorkflowDefinition(
            "custom", "test", 1,
            (StageDefinition("a", "work"), StageDefinition("a", "work")),
        )
        with self.assertRaisesRegex(WorkflowDefinitionError, "unique"):
            WorkflowEngine(registry, lambda _state: None).execute(duplicate_stage, WorkflowContext("wf", "task"))
        empty = WorkflowDefinition("custom", "test", 1, ())
        with self.assertRaisesRegex(WorkflowDefinitionError, "at least one stage"):
            WorkflowEngine(registry, lambda _state: None).execute(empty, WorkflowContext("wf", "task"))

    def test_false_condition_is_not_applicable_and_explicit_skip_stays_skipped(self):
        order: list[str] = []
        registry = StageRegistry()
        registry.register("check", RecordingStage("check", order, StageResult(output={"has_findings": False})))
        registry.register("fix", RecordingStage("fix", order))
        registry.register("optional", RecordingStage("optional", order))
        definition = WorkflowDefinition("conditional", "test", 1, (
            StageDefinition("review", "check"),
            StageDefinition(
                "correction", "fix", (StageDependency("review"),),
                run_when=StageCondition("review", ("has_findings",), "truthy"),
            ),
            StageDefinition("optional", "optional", optional=True, allow_skip=True),
        ))
        engine = WorkflowEngine(registry, lambda _state: None)
        state = {"definition": definition_snapshot(definition),
                 "status": "running", "stages": {}}
        context = WorkflowContext("wf", "task")
        engine.skip(definition, state, "optional", context)
        result = engine.execute(definition, context, state)
        self.assertEqual(order, ["check"])
        self.assertEqual(result["stages"]["correction"]["status"], "not_applicable")
        self.assertEqual(result["stages"]["optional"]["status"], "skipped")
        self.assertNotEqual(result["stages"]["optional"]["status"], "completed")

    def test_warn_failure_keeps_failed_status_and_allows_dependents_to_continue(self):
        order: list[str] = []

        class Fails(RecordingStage):
            def execute(self, stage_input):
                self.order.append(self.name)
                return StageResult(status=StageStatus.FAILED, error="provider unavailable")

        registry = StageRegistry()
        registry.register("optional", Fails("optional", order))
        registry.register("next", RecordingStage("next", order))
        definition = WorkflowDefinition("warn", "test", 1, (
            StageDefinition("optional", "optional", optional=True),
            StageDefinition("next", "next", (StageDependency("optional"),)),
        ), failure_policy="warn")

        state = WorkflowEngine(registry, lambda _state: None).execute(
            definition, WorkflowContext("wf", "task"),
        )

        self.assertEqual(state["status"], "complete")
        self.assertEqual(state["stages"]["optional"]["status"], "failed")
        self.assertEqual(state["stages"]["next"]["status"], "completed")
        self.assertEqual(state["warnings"], ["optional"])
        self.assertEqual(order, ["optional", "next"])

    def test_skipped_stage_input_is_recorded_and_stale_skip_is_invalidated(self):
        order: list[str] = []
        registry = StageRegistry()
        registry.register("optional", RecordingStage("optional", order))
        registry.register("next", RecordingStage("next", order))
        definition = WorkflowDefinition("skip-fingerprint", "test", 1, (
            StageDefinition("optional", "optional", optional=True, allow_skip=True),
            StageDefinition("next", "next", (StageDependency("optional", accept_skipped=True),)),
        ))
        engine = WorkflowEngine(registry, lambda _state: None)
        context = WorkflowContext("wf", "task", {"revision": 1})
        state = {"definition": definition_snapshot(definition), "status": "running", "stages": {}}
        engine.skip(definition, state, "optional", context)
        completed = engine.execute(definition, context, state)
        self.assertEqual(completed["stages"]["optional"]["status"], "skipped")
        self.assertTrue(completed["stages"]["optional"].get("input_sha256"))

        stale = engine.execute(definition, WorkflowContext("wf", "task", {"revision": 2}), completed)

        self.assertEqual(stale["status"], "blocked")
        self.assertTrue(stale["stages"]["optional"].get("rerun_required"))
        self.assertEqual(order, ["next"])

    def test_retry_reuses_completed_predecessor_and_invalidates_dependents(self):
        order: list[str] = []
        failures = {"implementation": 1}

        class SometimesFails(RecordingStage):
            def execute(self, stage_input):
                self.order.append(self.name)
                if failures[self.name]:
                    failures[self.name] -= 1
                    raise RuntimeError("transient test failure")
                return self.result

        registry = StageRegistry()
        registry.register("context", RecordingStage("context", order))
        registry.register("implementation", SometimesFails("implementation", order))
        registry.register("testing", RecordingStage("testing", order))
        saved_state_keys: list[set[str]] = []
        definition = WorkflowDefinition("retry", "test", 1, (
            StageDefinition("context", "context"),
            StageDefinition("implementation", "implementation", (StageDependency("context"),)),
            StageDefinition("testing", "testing", (StageDependency("implementation"),)),
        ))
        engine = WorkflowEngine(registry, lambda current: saved_state_keys.append(set(current)))
        state = engine.execute(definition, WorkflowContext("wf", "task"))
        self.assertEqual(state["status"], "failed")
        self.assertNotIn("transient test failure", state["stages"]["implementation"]["error"])
        with self.assertRaisesRegex(WorkflowDefinitionError, "recovery did not authorize"):
            engine.execute(definition, WorkflowContext("wf", "task"), state, retry_failed=True)
        state = engine.execute(
            definition, WorkflowContext("wf", "task"), state,
            retry_failed=True, retry_allowed=lambda _stage_id, _record: True,
        )
        self.assertEqual(state["status"], "complete")
        self.assertEqual(order, ["context", "implementation", "implementation", "testing"])
        self.assertEqual(len(state["stages"]["implementation"]["attempts"]), 2)
        self.assertTrue(all("definition" in keys and "stages" in keys for keys in saved_state_keys))
        invalidated = engine.invalidate_dependents(definition, state, "implementation")
        self.assertEqual(invalidated, {"testing"})
        self.assertEqual(state["stages"]["testing"]["status"], "pending")

    def test_changed_stage_inputs_invalidate_completed_evidence(self):
        order: list[str] = []
        registry = StageRegistry()
        registry.register("work", RecordingStage("work", order))
        definition = WorkflowDefinition("fingerprint", "test", 1, (StageDefinition("work", "work"),))
        engine = WorkflowEngine(registry, lambda _state: None)
        first = engine.execute(definition, WorkflowContext("wf", "task", {"revision": 1}))
        second = engine.execute(
            definition, WorkflowContext("wf", "task", {"revision": 2}), first,
        )
        self.assertEqual(order, ["work"])
        self.assertEqual(second["stages"]["work"]["status"], "blocked")
        second = engine.execute(
            definition, WorkflowContext("wf", "task", {"revision": 2}), second,
            rerun_allowed=lambda _stage_id, _record: True,
        )
        self.assertEqual(order, ["work", "work"])
        self.assertEqual(second["stages"]["work"]["status"], "completed")
        self.assertEqual(len(second["stages"]["work"]["attempts"]), 2)

    def test_interrupted_running_stage_blocks_until_recovery_resolves_it(self):
        order: list[str] = []
        registry = StageRegistry()
        registry.register("work", RecordingStage("work", order))
        definition = WorkflowDefinition("interrupted", "test", 1, (StageDefinition("work", "work"),))
        state = {"definition": definition_snapshot(definition), "status": "running", "stages": {}}

        def interrupt_after_start(current):
            if current["stages"].get("work", {}).get("status") == "running":
                raise KeyboardInterrupt()

        engine = WorkflowEngine(registry, interrupt_after_start)
        with self.assertRaises(KeyboardInterrupt):
            engine.execute(definition, WorkflowContext("wf", "task"), state)
        blocked = engine.execute(definition, WorkflowContext("wf", "task"), state)
        self.assertEqual(blocked["status"], "blocked")
        self.assertEqual(blocked["stages"]["work"]["status"], "blocked")
        self.assertEqual(order, [])

    def test_invalidation_cannot_clear_an_uncertain_running_dependent(self):
        registry = StageRegistry()
        registry.register("stage", RecordingStage("work", []))
        definition = WorkflowDefinition("invalidation", "test", 1, (
            StageDefinition("first", "stage"),
            StageDefinition("second", "stage", (StageDependency("first"),)),
        ))
        state = {
            "definition": definition_snapshot(definition), "status": "running",
            "stages": {
                "first": {"status": "completed", "attempts": [], "output": {"value": 1}},
                "second": {"status": "running", "attempts": [{"attempt": 1, "status": "running"}]},
            },
        }
        engine = WorkflowEngine(registry, lambda _state: None)
        with self.assertRaisesRegex(WorkflowDefinitionError, "recovery state is resolved"):
            engine.invalidate_dependents(definition, state, "first")
        self.assertEqual(state["stages"]["second"]["status"], "running")

    def test_required_stage_cannot_be_skipped_and_required_dependency_blocks(self):
        registry = StageRegistry()
        registry.register("stage", RecordingStage("stage", []))
        definition = WorkflowDefinition("required", "test", 1, (
            StageDefinition("required", "stage"),
            StageDefinition("next", "stage", (StageDependency("required"),)),
        ))
        engine = WorkflowEngine(registry, lambda _state: None)
        with self.assertRaisesRegex(WorkflowDefinitionError, "cannot be skipped"):
            engine.skip(
                definition,
                {"definition": definition_snapshot(definition), "stages": {}},
                "required",
                WorkflowContext("wf", "task"),
            )

    def test_malformed_saved_stage_state_fails_closed(self):
        registry = StageRegistry()
        registry.register("stage", RecordingStage("work", []))
        definition = WorkflowDefinition("state", "test", 1, (StageDefinition("work", "stage"),))
        engine = WorkflowEngine(registry, lambda _state: None)
        state = {
            "definition": definition_snapshot(definition), "status": "running",
            "stages": {"work": {"status": "passed", "attempts": []}},
        }
        with self.assertRaisesRegex(WorkflowDefinitionError, "status.*invalid"):
            engine.execute(definition, WorkflowContext("wf", "task"), state)

    def test_ask_failure_policy_blocks_instead_of_reporting_completion(self):
        registry = StageRegistry()
        calls = []

        class Recovers:
            def execute(self, stage_input):
                calls.append("called")
                if len(calls) == 1:
                    return StageResult(status=StageStatus.FAILED, error="provider unavailable")
                return StageResult(output={"recovered": True})

        registry.register("stage", Recovers())
        definition = WorkflowDefinition(
            "ask-on-failure", "test", 1, (StageDefinition("work", "stage"),), failure_policy="ask",
        )
        state = WorkflowEngine(registry, lambda _state: None).execute(definition, WorkflowContext("wf", "task"))
        self.assertEqual(state["status"], "blocked")
        self.assertEqual(state["stages"]["work"]["status"], "blocked")
        self.assertEqual(state["stages"]["work"]["attempts"][0]["status"], "failed")
        state = WorkflowEngine(registry, lambda _state: None).execute(
            definition, WorkflowContext("wf", "task"), state,
            retry_failed=True, retry_allowed=lambda _stage_id, _record: True,
        )
        self.assertEqual(state["status"], "complete")
        self.assertEqual(len(state["stages"]["work"]["attempts"]), 2)

    def test_profiles_resolve_full_flow_and_reject_policy_bypasses(self):
        constraints = RepositoryWorkflowConstraints()
        self.assertEqual(resolve_workflow("full-development", constraints), BUILT_IN_WORKFLOWS["full-development"])
        for profile in ("bugfix", "quick-fix", "implementation-only"):
            with self.subTest(profile=profile), self.assertRaisesRegex(WorkflowDefinitionError, "conflicts with repository policy"):
                resolve_workflow(profile, constraints)

    def test_assessment_is_explainable_and_reassessment_cannot_downgrade(self):
        low = assess_task("Fix a typo in one local comment.")
        high = assess_task("Change authentication and credential handling across the public API.")
        escalated = reassess_task(low, "Change authentication and credential handling across the public API.")
        self.assertEqual(low.risk, "low")
        self.assertEqual(high.risk, "high")
        self.assertEqual(escalated.risk, "high")
        self.assertTrue(escalated.approval_recommended)
        self.assertTrue(escalated.reasons)

    def test_reassessment_uses_explicit_plan_operations_not_plan_length(self):
        initial = assess_task("Fix one local typo.")
        long_generic_plan = "Security, migrations, and integrations are considered as future risks. " * 200

        unchanged = reassess_task(initial, "Fix one local typo.", plan_text=long_generic_plan)
        escalated = reassess_task(
            initial, "Fix one local typo.", plan_text="The approved plan will change authentication behavior.",
        )

        self.assertEqual((unchanged.complexity, unchanged.risk), ("low", "low"))
        self.assertEqual(escalated.risk, "high")
        self.assertTrue(escalated.approval_recommended)

    def test_persisted_assessment_requires_all_policy_fields(self):
        with self.assertRaisesRegex(ValueError, "incomplete"):
            assessment_from_dict({"complexity": "low"})

    def test_policy_resolver_keeps_repository_approval_and_proposal_gates(self):
        constraints = RepositoryWorkflowConstraints()
        assessment = assess_task("Fix a typo.")
        with self.assertRaisesRegex(ValueError, "requires a proposal"):
            resolve_policies(
                proposal_policy="disabled", approval_policy="always", assessment=assessment,
                constraints=constraints,
            )
        with self.assertRaisesRegex(ValueError, "requires explicit human approval"):
            resolve_policies(
                proposal_policy="required", approval_policy="never", assessment=assessment,
                constraints=constraints,
            )
        decision = resolve_policies(
            proposal_policy="required", approval_policy="always", assessment=assessment,
            constraints=constraints,
        )
        self.assertTrue(decision.proposal_required)
        self.assertTrue(decision.approval_required)


if __name__ == "__main__":
    unittest.main()
