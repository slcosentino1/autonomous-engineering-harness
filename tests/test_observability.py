from __future__ import annotations

import os
import json
import subprocess
import sys
import tempfile
import unittest
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

HARNESS = Path(__file__).resolve().parents[1] / "harness"
if str(HARNESS) not in sys.path:
    sys.path.insert(0, str(HARNESS))

import orchestrator
from observability import langfuse
from observability.tracer import (
    NoOpTracer,
    base_url_digest,
    isolated_context,
    observability_scope,
    safe_workflow_reference,
    tracing_enabled_from_environment,
)
from workflow import cli
from workflow.review import ReviewCampaign


class FakeRunner:
    def __init__(self, *_args, **kwargs):
        self.budget_usd = kwargs.get("budget_usd", 2.0)
        self.max_code_review_calls = kwargs.get("max_code_review_calls", 16)
        self.tracing_enabled = kwargs.get("tracing_enabled", False)

    def start(self):
        return {"workflow_id": "trace-test", "status": "awaiting_approval", "stage": "implementation"}

    def status(self):
        return {"workflow_id": "trace-test", "status": "awaiting_approval", "stage": "implementation"}


class FakeClient:
    def __init__(self):
        self.observations = []
        self.flush_called = False
        self.shutdown_called = False

    def start_as_current_observation(self, *, name, as_type, metadata, **kwargs):
        client = self

        @contextmanager
        def observation():
            client.observations.append((name, as_type, dict(metadata), dict(kwargs)))
            yield object()

        return observation()

    def flush(self):
        self.flush_called = True

    def shutdown(self):
        self.shutdown_called = True


class ObservabilityTests(unittest.TestCase):
    @staticmethod
    def _make_target(path: Path) -> Path:
        path.mkdir()
        subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True)
        subprocess.run(["git", "-C", str(path), "config", "user.name", "Harness Test"], check=True)
        subprocess.run(["git", "-C", str(path), "config", "user.email", "harness-test@example.invalid"], check=True)
        (path / "README.md").write_text("test target\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(path), "add", "README.md"], check=True)
        subprocess.run(["git", "-C", str(path), "commit", "-q", "-m", "baseline"], check=True)
        return path

    @staticmethod
    def _saved_runner(
        target: Path,
        workflow_id: str,
        *,
        status: str = "awaiting_approval",
        stage: str = "implementation",
        tracing_enabled: bool = True,
        legacy_scope: bool = False,
    ):
        runner = orchestrator.Orchestrator(target, workflow_id=workflow_id)
        runner.workflow_dir.mkdir(parents=True)
        authorization = orchestrator.new_workflow_authorization(
            target.resolve(), workflow_id, 2.0, tracing_enabled=tracing_enabled,
        )
        if legacy_scope:
            authorization.pop("observability")
            authorization["scope_sha256"] = orchestrator.authorization_scope_digest(authorization)
        proposal = "# Proposal\n\nA saved proposal for this test.\n"
        state = {
            "version": 1,
            "workflow_id": workflow_id,
            "target": str(target.resolve()),
            "task": "test task",
            "status": status,
            "stage": stage,
            "proposal": proposal,
            "proposal_version": 1,
            "project_guidance": {},
            "plan_reviews": [],
            "authorization": authorization,
        }
        runner.state_path.write_text(json.dumps(state), encoding="utf-8")
        runner.proposal_path.write_text(proposal, encoding="utf-8")
        return runner, state

    @staticmethod
    def _run_cli(command, runner):
        args = [command, "--target", str(runner.target), "--workflow-id", runner.workflow_id]
        if command == "revise":
            args.extend(["--feedback", "Clarify the proposal."])
        with redirect_stdout(StringIO()):
            return cli.main(
                args,
                runner_factory=lambda *_args, **_kwargs: runner,
                recover_review=lambda *_args, **_kwargs: {"errors": []},
                workflow_error=orchestrator.WorkflowError,
                workflow_root=".engineering/workflows",
                max_authorized_code_review_calls=64,
            )

    def test_tracing_is_disabled_by_default_and_environment_is_explicit(self):
        self.assertFalse(tracing_enabled_from_environment({}))
        self.assertFalse(tracing_enabled_from_environment({"HARNESS_TRACING_ENABLED": "false"}))
        self.assertTrue(tracing_enabled_from_environment({"HARNESS_TRACING_ENABLED": "true"}))
        self.assertTrue(tracing_enabled_from_environment({"HARNESS_TRACING_ENABLED": "1"}))

    def test_cli_trace_flag_and_environment_enable_only_new_starts(self):
        imports = []
        initialization_commands = []

        class ReadySDK:
            @staticmethod
            def Langfuse(**_kwargs):
                return FakeClient()

            @staticmethod
            def propagate_attributes(**_kwargs):
                raise AssertionError("preflight must not send or create a trace")

        def run(args, environ):
            created = []

            def factory(*factory_args, **kwargs):
                runner = FakeRunner(*factory_args, **kwargs)
                created.append(runner)
                return runner

            configured = dict(environ)
            explicit_trace = "--trace" in args
            if explicit_trace:
                configured.setdefault("LANGFUSE_PUBLIC_KEY", "public")
                configured.setdefault("LANGFUSE_SECRET_KEY", "secret")
            if explicit_trace:
                importer = patch.object(
                    langfuse.importlib, "import_module",
                    side_effect=lambda name: (imports.append(name), ReadySDK())[1],
                )
                initialize = patch.object(
                    langfuse.subprocess,
                    "run",
                    side_effect=lambda command, **_kwargs: (
                        initialization_commands.append(command),
                        type("Result", (), {"returncode": 0})(),
                    )[1],
                )
            else:
                importer = patch.object(
                    langfuse.importlib, "import_module",
                    side_effect=AssertionError("unexpected SDK check"),
                )
                initialize = patch.object(
                    langfuse.subprocess, "run", side_effect=AssertionError("unexpected client initialization"),
                )
            with patch.dict(os.environ, configured, clear=True), importer, initialize:
                with redirect_stdout(StringIO()):
                    result = cli.main(
                        ["start", "--target", "/tmp/target", "--task", "safe task", *args],
                        runner_factory=factory,
                        recover_review=lambda *_args, **_kwargs: {},
                        workflow_error=RuntimeError,
                        workflow_root=".engineering/workflows",
                        max_authorized_code_review_calls=64,
                    )
            self.assertEqual(result, 0)
            return created[0]

        self.assertTrue(run(["--trace"], {}).tracing_enabled)
        self.assertTrue(run([], {"HARNESS_TRACING_ENABLED": "true"}).tracing_enabled)
        self.assertFalse(run([], {"LANGFUSE_PUBLIC_KEY": "present", "LANGFUSE_SECRET_KEY": "present"}).tracing_enabled)
        self.assertEqual(imports, ["langfuse"])
        self.assertEqual(initialization_commands[0][0], sys.executable)
        self.assertIn("-c", initialization_commands[0])

    def test_cli_status_never_runs_langfuse_preflight(self):
        with patch.dict(os.environ, {"HARNESS_TRACING_ENABLED": "true"}, clear=True):
            with patch.object(langfuse.importlib, "import_module", side_effect=AssertionError("unexpected SDK check")):
                with redirect_stdout(StringIO()):
                    result = cli.main(
                        ["status", "--target", "/tmp/target", "--workflow-id", "trace-test"],
                        runner_factory=FakeRunner,
                        recover_review=lambda *_args, **_kwargs: {},
                        workflow_error=RuntimeError,
                        workflow_root=".engineering/workflows",
                        max_authorized_code_review_calls=64,
                    )
        self.assertEqual(result, 0)

    def test_saved_tracing_preflight_runs_for_approve_resume_and_revise_before_execution(self):
        events = []
        with tempfile.TemporaryDirectory(prefix="harness-tracing-preflight-") as temp:
            target = self._make_target(Path(temp) / "target")
            for command in ("approve", "resume", "revise"):
                status = "failed" if command == "resume" else "awaiting_approval"
                stage = "implementation" if command == "resume" else "implementation"
                runner, _state = self._saved_runner(
                    target, f"trace-{command}", status=status, stage=stage, tracing_enabled=True,
                )

                def execute(runner=runner):
                    events.append("workflow_execution")
                    runner.state["status"] = "awaiting_approval"
                    return runner.state

                runner.run_pending = execute
                runner._begin_execution_trace = lambda _command: events.append("begin_trace")
                with patch.dict(os.environ, {}, clear=True), \
                        patch.object(langfuse, "preflight_langfuse_tracing",
                                     side_effect=lambda: events.append("preflight")), \
                        patch("orchestrator._project_guidance", return_value={}):
                    result = self._run_cli(command, runner)
                self.assertEqual(result, 0)
                self.assertEqual(events, ["preflight", "begin_trace", "workflow_execution"])
                events.clear()

    def test_disabled_and_legacy_saved_workflows_do_not_preflight(self):
        with tempfile.TemporaryDirectory(prefix="harness-tracing-disabled-") as temp:
            target = self._make_target(Path(temp) / "target")
            cases = (
                ("approve", False, False), ("resume", False, False), ("revise", False, False),
                ("approve", True, True), ("resume", True, True), ("revise", True, True),
            )
            for index, (command, enabled, legacy) in enumerate(cases):
                status = "failed" if command == "resume" else "awaiting_approval"
                runner, _state = self._saved_runner(
                    target, f"no-trace-{index}", status=status, tracing_enabled=enabled, legacy_scope=legacy,
                )
                execution = []

                def execute(runner=runner):
                    execution.append(True)
                    runner.state["status"] = "awaiting_approval"
                    return runner.state

                runner.run_pending = execute
                runner._begin_execution_trace = lambda _command: None
                with patch.object(langfuse, "preflight_langfuse_tracing",
                                  side_effect=AssertionError("disabled workflow must not preflight")), \
                        patch("orchestrator._project_guidance", return_value={}):
                    result = self._run_cli(command, runner)
                self.assertEqual(result, 0)
                self.assertEqual(execution, [True])

    def test_saved_tracing_preflight_failure_stops_before_provider_execution(self):
        with tempfile.TemporaryDirectory(prefix="harness-tracing-preflight-fail-") as temp:
            target = self._make_target(Path(temp) / "target")
            runner, _state = self._saved_runner(target, "preflight-failure", tracing_enabled=True)
            runner.run_pending = lambda: self.fail("provider execution must not start")
            before = runner.state_path.read_bytes()
            stderr = StringIO()
            with patch.dict(os.environ, {}, clear=True), \
                    patch.object(langfuse, "preflight_langfuse_tracing",
                                 side_effect=langfuse.LangfusePreflightError("SDK unavailable")), \
                    redirect_stderr(stderr):
                result = cli.main(
                    ["approve", "--target", str(target), "--workflow-id", runner.workflow_id],
                    runner_factory=lambda *_args, **_kwargs: runner,
                    recover_review=lambda *_args, **_kwargs: {"errors": []},
                    workflow_error=orchestrator.WorkflowError,
                    workflow_root=".engineering/workflows",
                    max_authorized_code_review_calls=64,
                )
            self.assertEqual(result, 2)
            self.assertIn("SDK unavailable", stderr.getvalue())
            self.assertEqual(runner.state_path.read_bytes(), before)

    def test_status_reject_and_recover_review_never_run_saved_scope_preflight(self):
        with tempfile.TemporaryDirectory(prefix="harness-tracing-nonexecution-") as temp:
            target = self._make_target(Path(temp) / "target")
            for command in ("status", "reject", "recover-review"):
                runner, _state = self._saved_runner(target, f"no-preflight-{command}", tracing_enabled=True)
                runner._begin_execution_trace = lambda _command: None
                args = [command, "--target", str(target), "--workflow-id", runner.workflow_id]
                with patch.dict(os.environ, {"HARNESS_TRACING_ENABLED": "true"}, clear=True), \
                        patch.object(langfuse, "preflight_langfuse_tracing",
                                     side_effect=AssertionError(f"{command} must not preflight")), \
                        redirect_stdout(StringIO()):
                    result = cli.main(
                        args,
                        runner_factory=lambda *_args, **_kwargs: runner,
                        recover_review=lambda *_args, **_kwargs: {"errors": []},
                        workflow_error=orchestrator.WorkflowError,
                        workflow_root=".engineering/workflows",
                        max_authorized_code_review_calls=64,
                    )
                self.assertEqual(result, 0)

    def test_missing_sdk_preflight_stops_before_workflow_factory_and_suggests_one_time_install(self):
        created = []
        stderr = StringIO()

        def factory(*_args, **_kwargs):
            created.append(True)
            return FakeRunner()

        with patch.dict(os.environ, {}, clear=True):
            with patch.object(
                langfuse.importlib, "import_module", side_effect=ModuleNotFoundError("langfuse"),
            ):
                with redirect_stderr(stderr):
                    result = cli.main(
                        ["start", "--target", "/tmp/target", "--task", "safe task", "--trace"],
                        runner_factory=factory,
                        recover_review=lambda *_args, **_kwargs: {},
                        workflow_error=orchestrator.WorkflowError,
                        workflow_root=".engineering/workflows",
                        max_authorized_code_review_calls=64,
                    )
        self.assertEqual(result, 2)
        self.assertEqual(created, [])
        message = stderr.getvalue()
        self.assertIn("current Python environment", message)
        self.assertIn("python -m pip install -r requirements-tracing.txt", message)
        self.assertIn("once", message)
        self.assertIn("No workflow was started", message)

    def test_preflight_checks_credentials_and_endpoint_without_initializing_when_invalid(self):
        created = []

        class SDK:
            @staticmethod
            def Langfuse(**_kwargs):
                created.append(True)

            propagate_attributes = object()

        with self.assertRaisesRegex(langfuse.LangfusePreflightError, "credentials are incomplete"):
            langfuse.preflight_langfuse_tracing(sdk_loader=lambda: SDK, environ={})
        self.assertEqual(created, [])

        configured = {"LANGFUSE_PUBLIC_KEY": "public", "LANGFUSE_SECRET_KEY": "secret",
                      "LANGFUSE_BASE_URL": "not-a-url"}
        with self.assertRaisesRegex(langfuse.LangfusePreflightError, "LANGFUSE_BASE_URL is invalid"):
            langfuse.preflight_langfuse_tracing(sdk_loader=lambda: SDK, environ=configured)
        self.assertEqual(created, [])

    def test_missing_credentials_stop_cli_before_workflow_creation(self):
        class SDK:
            @staticmethod
            def Langfuse(**_kwargs):
                raise AssertionError("client must not be initialized without credentials")

            propagate_attributes = lambda **_kwargs: None

        stderr = StringIO()
        with patch.dict(os.environ, {}, clear=True):
            with patch.object(langfuse.importlib, "import_module", return_value=SDK()):
                with redirect_stderr(stderr):
                    result = cli.main(
                        ["start", "--target", "/tmp/target", "--task", "safe task", "--trace"],
                        runner_factory=lambda *_args, **_kwargs: self.fail("workflow must not start"),
                        recover_review=lambda *_args, **_kwargs: {},
                        workflow_error=orchestrator.WorkflowError,
                        workflow_root=".engineering/workflows",
                        max_authorized_code_review_calls=64,
                    )
        self.assertEqual(result, 2)
        self.assertIn("credentials are incomplete", stderr.getvalue())
        self.assertIn("LANGFUSE_PUBLIC_KEY", stderr.getvalue())
        self.assertIn("LANGFUSE_SECRET_KEY", stderr.getvalue())
        self.assertIn("No workflow was started", stderr.getvalue())

    def test_client_initialization_failure_aborts_explicit_trace_without_leaking_secrets(self):
        class BrokenSDK:
            @staticmethod
            def Langfuse(**_kwargs):
                raise RuntimeError("private initialization detail with SECRET_VALUE")

            propagate_attributes = lambda **_kwargs: None

        stderr = StringIO()
        with patch.dict(os.environ, {
            "LANGFUSE_PUBLIC_KEY": "public", "LANGFUSE_SECRET_KEY": "SECRET_VALUE",
        }, clear=True):
            with patch.object(langfuse.importlib, "import_module", return_value=BrokenSDK()):
                with patch.object(
                    langfuse.subprocess, "run",
                    return_value=type("Result", (), {"returncode": 1, "stderr": "SECRET_VALUE"})(),
                ):
                    with redirect_stderr(stderr):
                        result = cli.main(
                            ["start", "--target", "/tmp/target", "--task", "safe task", "--trace"],
                            runner_factory=lambda *_args, **_kwargs: self.fail("workflow must not start"),
                            recover_review=lambda *_args, **_kwargs: {},
                            workflow_error=orchestrator.WorkflowError,
                            workflow_root=".engineering/workflows",
                            max_authorized_code_review_calls=64,
                        )
        self.assertEqual(result, 2)
        self.assertIn("could not be initialized locally", stderr.getvalue())
        self.assertNotIn("SECRET_VALUE", stderr.getvalue())

    def test_valid_preflight_initializes_and_closes_client_before_start(self):
        initialization = []

        class SDK:
            @staticmethod
            def Langfuse(**_kwargs):
                return FakeClient()

            @staticmethod
            def propagate_attributes(**_kwargs):
                raise AssertionError("preflight must not test remote connectivity")

        created = []

        def factory(*factory_args, **kwargs):
            runner = FakeRunner(*factory_args, **kwargs)
            created.append(runner)
            return runner

        with patch.dict(os.environ, {
            "LANGFUSE_PUBLIC_KEY": "public", "LANGFUSE_SECRET_KEY": "secret",
            "LANGFUSE_BASE_URL": "https://trace.example.test",
        }, clear=True):
            with patch.object(langfuse.importlib, "import_module", return_value=SDK()):
                with patch.object(
                    langfuse.subprocess, "run",
                    side_effect=lambda command, **kwargs: (
                        initialization.append((command, kwargs)),
                        type("Result", (), {"returncode": 0})(),
                    )[1],
                ):
                    with redirect_stdout(StringIO()):
                        result = cli.main(
                            ["start", "--target", "/tmp/target", "--task", "safe task", "--trace"],
                            runner_factory=factory,
                            recover_review=lambda *_args, **_kwargs: {},
                            workflow_error=orchestrator.WorkflowError,
                            workflow_root=".engineering/workflows",
                            max_authorized_code_review_calls=64,
                        )
        self.assertEqual(result, 0)
        self.assertEqual(len(created), 1)
        self.assertTrue(created[0].tracing_enabled)
        self.assertEqual(initialization[0][0][0], sys.executable)
        self.assertEqual(initialization[0][1]["env"]["LANGFUSE_BASE_URL"], "https://trace.example.test")
        self.assertIn("shutdown()", initialization[0][0][-1])

    def test_disabled_scope_returns_noop_without_importing_langfuse(self):
        with patch.object(langfuse.importlib, "import_module", side_effect=AssertionError("unexpected SDK import")):
            tracer = langfuse.create_langfuse_tracer(observability_scope(False), environ={})
            self.assertIsInstance(tracer, NoOpTracer)
            with tracer.execution(name="workflow.execution", session_id="session", metadata={"command": "start"}):
                with tracer.span(name="workflow.stage", metadata={"stage": "planning"}):
                    result = "workflow result"
            tracer.flush()
        self.assertEqual(result, "workflow result")

    def test_missing_credentials_warns_and_uses_noop(self):
        warnings = []
        env = {"LANGFUSE_BASE_URL": "https://trace.example.test"}
        scope = observability_scope(True, env)
        tracer = langfuse.create_langfuse_tracer(
            scope, environ=env, warning_sink=lambda message, **_kwargs: warnings.append(message),
        )
        self.assertIsInstance(tracer, NoOpTracer)
        self.assertEqual(len(warnings), 1)
        self.assertIn("credentials are unavailable", warnings[0])

    def test_endpoint_is_pinned_without_persisting_the_url(self):
        first = observability_scope(True, {"LANGFUSE_BASE_URL": "https://trace.example.test/"})
        second = observability_scope(True, {"LANGFUSE_BASE_URL": "https://other.example.test"})
        self.assertEqual(first["langfuse_base_url_sha256"], base_url_digest("https://trace.example.test"))
        self.assertNotIn("trace.example.test", str(first))
        self.assertNotEqual(first["langfuse_base_url_sha256"], second["langfuse_base_url_sha256"])
        warnings = []
        tracer = langfuse.create_langfuse_tracer(
            first,
            environ={"LANGFUSE_BASE_URL": "https://other.example.test", "LANGFUSE_PUBLIC_KEY": "p", "LANGFUSE_SECRET_KEY": "s"},
            warning_sink=lambda message, **_kwargs: warnings.append(message),
        )
        self.assertIsInstance(tracer, NoOpTracer)
        self.assertEqual(len(warnings), 1)

    def test_missing_sdk_and_initialization_errors_warn_without_raising(self):
        tracer = langfuse.BufferedLangfuseTracer(
            session_id="session", public_key="public", secret_key="secret", base_url="https://cloud.langfuse.com",
        )
        with tracer.execution(name="workflow.execution", session_id="session", metadata={"command": "start"}):
            pass
        stderr = StringIO()
        with patch.object(langfuse.importlib, "import_module", side_effect=ModuleNotFoundError("langfuse")), redirect_stderr(stderr):
            tracer.flush()
        self.assertIn("initialization or export failed", stderr.getvalue())

        class BrokenSDK:
            def Langfuse(self, **_kwargs):
                raise RuntimeError("contains a secret and must not be logged")

        stderr = StringIO()
        with patch.object(langfuse.importlib, "import_module", return_value=BrokenSDK()), redirect_stderr(stderr):
            tracer.flush()
        self.assertIn("initialization or export failed", stderr.getvalue())
        self.assertNotIn("secret", stderr.getvalue())

    def test_sdk_span_export_failure_and_flush_shutdown_failures_are_isolated(self):
        tracer = langfuse.BufferedLangfuseTracer(
            session_id="session", public_key="public", secret_key="secret", base_url="https://cloud.langfuse.com",
        )
        with tracer.execution(name="workflow.execution", session_id="session", metadata={"command": "start"}):
            with tracer.span(name="workflow.stage", metadata={"stage": "planning"}):
                pass

        class BrokenClient(FakeClient):
            def start_as_current_observation(self, **_kwargs):
                raise RuntimeError("unsafe provider diagnostic")

            def flush(self):
                raise RuntimeError("unsafe flush diagnostic")

            def shutdown(self):
                raise RuntimeError("unsafe shutdown diagnostic")

        class SDK:
            @staticmethod
            @contextmanager
            def propagate_attributes(**_kwargs):
                yield

            @staticmethod
            def Langfuse(**_kwargs):
                return BrokenClient()

        stderr = StringIO()
        with patch.object(langfuse.importlib, "import_module", return_value=SDK()), redirect_stderr(stderr):
            tracer.flush()
        self.assertGreaterEqual(stderr.getvalue().count("Tracing warning:"), 2)
        self.assertNotIn("unsafe", stderr.getvalue())

    def test_export_arguments_contain_only_harness_allowlisted_metadata(self):
        tracer = langfuse.BufferedLangfuseTracer(
            session_id="session", public_key="public", secret_key="secret", base_url="https://cloud.langfuse.com",
        )
        with tracer.execution(
            name="workflow.execution",
            session_id="session",
            metadata={
                "command": "start",
                "workflow_ref": "abc123",
                "prompt": "PRIVATE_PROMPT",
                "source": "PRIVATE_SOURCE",
                "secret": "PRIVATE_SECRET",
                "path": "/private/workspace",
            },
        ):
            with tracer.span(
                name="provider.claude_review",
                metadata={"provider": "claude", "operation": "plan_review", "report": "PRIVATE_REPORT"},
            ):
                pass
        client = FakeClient()

        class SDK:
            @staticmethod
            @contextmanager
            def propagate_attributes(**_kwargs):
                yield

            @staticmethod
            def Langfuse(**_kwargs):
                return client

        with patch.object(langfuse.importlib, "import_module", return_value=SDK()):
            tracer.flush()
        payload = repr(client.observations)
        for secret in ("PRIVATE_PROMPT", "PRIVATE_SOURCE", "PRIVATE_SECRET", "PRIVATE_REPORT", "/private/workspace"):
            self.assertNotIn(secret, payload)
        self.assertEqual([event[0] for event in client.observations], ["workflow.execution", "provider.claude_review"])
        root, review = client.observations
        harness_metadata_keys = {
            "command", "session_id", "initial_stage", "final_stage", "final_status",
            "execution_result", "duration_ms", "stage", "provider", "operation", "sandbox",
            "model", "iteration", "review_kind", "review_part_index", "review_part_count",
            "review_result", "cost_usd", "error_category", "retry_count", "attempt", "status",
        }
        for _name, _kind, metadata, _kwargs in client.observations:
            self.assertLessEqual(set(metadata), harness_metadata_keys)
        self.assertIn("Command: start", root[3]["input"])
        self.assertIn("Provider: claude", review[3]["input"])
        self.assertIn("Operation: plan_review", review[3]["input"])
        self.assertIn("Status: success", review[3]["output"])
        self.assertIs(type(review[2]["duration_ms"]), int)
        self.assertTrue(client.flush_called)
        self.assertTrue(client.shutdown_called)

    def test_buffered_durations_use_monotonic_clock_for_root_and_child(self):
        tracer = langfuse.BufferedLangfuseTracer(
            session_id="stable-session", public_key="public", secret_key="secret",
            base_url="https://cloud.langfuse.com",
        )
        monotonic_values = iter((10.0, 10.05, 10.15, 10.2))
        with patch.object(langfuse.time, "monotonic", side_effect=lambda: next(monotonic_values)):
            with tracer.execution(
                name="workflow.execution", session_id="stable-session",
                metadata={"command": "approve", "session_id": "stable-session"},
            ):
                with tracer.generation(
                    name="provider.claude_review",
                    metadata={"provider": "claude", "stage": "code_review"},
                ):
                    pass

        client = FakeClient()

        class SDK:
            @staticmethod
            @contextmanager
            def propagate_attributes(**_kwargs):
                yield

            @staticmethod
            def Langfuse(**_kwargs):
                return client

        with patch.object(langfuse.importlib, "import_module", return_value=SDK()):
            tracer.flush()

        root, child = client.observations
        self.assertEqual(root[2]["duration_ms"], 200)
        self.assertEqual(child[2]["duration_ms"], 100)
        self.assertIs(type(root[2]["duration_ms"]), int)
        self.assertIs(type(child[2]["duration_ms"]), int)
        self.assertNotIn("start_time", root[3])
        self.assertNotIn("end_time", root[3])
        self.assertEqual(child[1], "generation")

    def test_claude_reported_cost_uses_native_generation_cost_without_inventing_usage(self):
        tracer = langfuse.BufferedLangfuseTracer(
            session_id="stable-session", public_key="public", secret_key="secret",
            base_url="https://cloud.langfuse.com",
        )
        with tracer.execution(
            name="workflow.execution", session_id="stable-session", metadata={"command": "approve"},
        ):
            with tracer.generation(
                name="provider.claude_review",
                metadata={"provider": "claude", "operation": "code_review"},
            ) as span:
                span.update(cost_usd=0.125)
            with tracer.generation(
                name="provider.codex",
                metadata={"provider": "codex", "model": "gpt-known-model"},
            ):
                pass
            with self.assertRaisesRegex(RuntimeError, "review failed"):
                with tracer.generation(
                    name="provider.claude_review",
                    metadata={"provider": "claude", "operation": "code_review"},
                ) as span:
                    span.update(cost_usd=0.025)
                    raise RuntimeError("review failed")

        client = FakeClient()

        class SDK:
            @staticmethod
            @contextmanager
            def propagate_attributes(**_kwargs):
                yield

            @staticmethod
            def Langfuse(**_kwargs):
                return client

        with patch.object(langfuse.importlib, "import_module", return_value=SDK()):
            tracer.flush()

        claude = client.observations[1]
        self.assertEqual(claude[1], "generation")
        self.assertNotIn("cost_usd", claude[2])
        self.assertEqual(claude[3]["cost_details"], {"total": 0.125})
        self.assertNotIn("usage_details", claude[3])
        self.assertNotIn("model", claude[3])
        self.assertIn("Cost: USD 0.125", claude[3]["output"])
        codex = client.observations[2]
        self.assertEqual(codex[3]["model"], "gpt-known-model")
        self.assertNotIn("model", codex[2])
        self.assertNotIn("cost_details", codex[3])
        failed_claude = client.observations[3]
        self.assertEqual(failed_claude[2]["status"], "failure")
        self.assertEqual(failed_claude[3]["cost_details"], {"total": 0.025})
        self.assertNotIn("cost_usd", failed_claude[2])

    def test_trace_session_reference_is_stable_across_resumed_executions(self):
        auth = orchestrator.new_workflow_authorization(Path("/target"), "same-workflow", 2.0, tracing_enabled=True)
        executions = []

        class RecordingTracer:
            def execution(self, *, name, session_id, metadata):
                executions.append((id(self), name, session_id, metadata))

                @contextmanager
                def context():
                    yield type("Span", (), {"update": lambda _self, **_kwargs: None})()

                return context()

            def span(self, *, name, metadata):
                return _empty_context()

            def flush(self):
                return None

        @contextmanager
        def _empty_context():
            yield None

        for command in ("start", "resume"):
            runner = object.__new__(orchestrator.Orchestrator)
            runner.state = {"authorization": auth, "stage": "planning", "status": "running"}
            runner.workflow_id = "same-workflow"
            runner._tracer = None
            runner._active_trace_context = None
            runner._trace_started_at = None
            with patch("observability.langfuse.create_langfuse_tracer", return_value=RecordingTracer()):
                runner._begin_execution_trace(command)
                runner._finish_execution_trace(None)
        self.assertEqual(len(executions), 2)
        self.assertEqual(executions[0][2], safe_workflow_reference("same-workflow"))
        self.assertEqual(executions[0][2], executions[1][2])
        self.assertEqual(executions[0][3]["session_id"], executions[0][2])
        self.assertNotIn("workflow_id", executions[0][3])
        self.assertNotEqual(executions[0][0], executions[1][0])

    def test_provider_generation_metadata_is_consistent_and_only_reports_known_models(self):
        recorded = []

        class RecordingSpan:
            def update(self, **metadata):
                recorded[-1]["updates"].append(metadata)

        class RecordingTracer:
            @contextmanager
            def generation(self, *, name, metadata):
                recorded.append({"name": name, "metadata": dict(metadata), "updates": []})
                yield RecordingSpan()

            def span(self, *, name, metadata):
                return self.generation(name=name, metadata=metadata)

        runner = object.__new__(orchestrator.Orchestrator)
        runner._tracer = RecordingTracer()
        runner.workflow_id = "provider-test"
        runner.state = {"stage": "implementation"}
        runner.model = "gpt-known-model"
        runner._run_codex_impl = lambda _prompt, *, sandbox: "done"
        self.assertEqual(runner._run_codex("private prompt", sandbox="workspace-write"), "done")

        codex = recorded[-1]["metadata"]
        self.assertEqual(codex["provider"], "codex")
        self.assertEqual(codex["stage"], "implementation")
        self.assertEqual(codex["model"], "gpt-known-model")
        self.assertEqual(codex["session_id"], safe_workflow_reference("provider-test"))
        self.assertNotIn("workflow_id", codex)

        runner.model = None
        runner.state = {"stage": "code_review"}
        runner._call_claude_impl = lambda *_args, **_kwargs: (
            "report", {"cost_usd": 0.125, "campaign_call_number": 2},
        )
        report, _manifest = runner._call_claude(
            "header", "code_review", {"section": "private"},
            metadata={"review_kind": "part", "part_id": "part-000003", "retry_count": 1},
        )
        self.assertEqual(report, "report")
        claude = recorded[-1]
        self.assertEqual(claude["metadata"]["provider"], "claude")
        self.assertEqual(claude["metadata"]["stage"], "code_review")
        self.assertEqual(claude["metadata"]["operation"], "code_review")
        self.assertEqual(claude["metadata"]["review_part_index"], 3)
        self.assertEqual(claude["metadata"]["retry_count"], 1)
        self.assertNotIn("model", claude["metadata"])
        self.assertIn({"cost_usd": 0.125}, claude["updates"])

        def failed_claude_call(*_args, **_kwargs):
            try:
                raise orchestrator.claude_reviewer.ReviewError("review failed", cost_usd=0.025)
            except orchestrator.claude_reviewer.ReviewError as cause:
                raise orchestrator.WorkflowError("review invocation failed") from cause

        runner._call_claude_impl = failed_claude_call
        with self.assertRaises(orchestrator.WorkflowError):
            runner._call_claude("header", "plan_review_1", {"section": "private"})
        self.assertIn({"cost_usd": 0.025}, recorded[-1]["updates"])

    def test_root_reports_harness_result_without_inventing_target_validation(self):
        auth = orchestrator.new_workflow_authorization(
            Path("/target"), "validation-test", 2.0, tracing_enabled=True,
        )
        updates = []
        executions = []

        class RootSpan:
            def update(self, **metadata):
                updates.append(metadata)

        class Tracer:
            def execution(self, *, name, session_id, metadata):
                executions.append((name, session_id, dict(metadata)))

                @contextmanager
                def context():
                    yield RootSpan()

                return context()

            def span(self, *, name, metadata):
                return _empty_context()

            def flush(self):
                pass

        @contextmanager
        def _empty_context():
            yield None

        runner = object.__new__(orchestrator.Orchestrator)
        runner.state = {
            "authorization": auth,
            "stage": "complete",
            "status": "complete",
            "implementation_summary": "No structured validation result is available.",
        }
        runner.workflow_id = "validation-test"
        runner._tracer = None
        runner._active_trace_context = None
        runner._trace_started_at = None
        with patch("observability.langfuse.create_langfuse_tracer", return_value=Tracer()), \
             patch.object(orchestrator.time, "monotonic", side_effect=(2.0, 2.2)):
            runner._begin_execution_trace("approve")
            runner._finish_execution_trace(None)

        self.assertEqual(executions[0][1], safe_workflow_reference("validation-test"))
        self.assertEqual(executions[0][2]["session_id"], executions[0][1])
        self.assertNotIn("workflow_id", executions[0][2])
        self.assertEqual(updates[0]["execution_result"], "success")
        self.assertEqual(updates[0]["duration_ms"], 200)
        self.assertNotIn("validation_status", updates[0])

    def test_observability_is_persisted_in_scope_digest_and_legacy_missing_scope_is_valid(self):
        authorization = orchestrator.new_workflow_authorization(
            Path("/target"), "workflow", 2.0, tracing_enabled=True,
        )
        self.assertTrue(authorization["observability"]["enabled"])
        self.assertEqual(authorization["scope_sha256"], orchestrator.authorization_scope_digest(authorization))
        authorization.pop("observability")
        authorization["scope_sha256"] = orchestrator.authorization_scope_digest(authorization)
        state = {"authorization": authorization}
        self.assertIs(
            orchestrator.validate_workflow_authorization(state, Path("/target"), "workflow"),
            authorization,
        )

    def test_tracing_failures_do_not_hide_operation_errors(self):
        class BrokenContext:
            def __enter__(self):
                raise RuntimeError("tracer enter failed")

            def __exit__(self, *_args):
                raise RuntimeError("tracer exit failed")

        class BrokenTracer:
            def span(self, **_kwargs):
                return BrokenContext()

        with isolated_context(lambda: BrokenTracer().span(name="stage", metadata={})):
            pass

        class BrokenUpdate:
            def update(self, **_kwargs):
                raise RuntimeError("tracer update failed")

        class UpdatingTracer:
            @contextmanager
            def span(self, **_kwargs):
                yield BrokenUpdate()

        with self.assertRaisesRegex(ValueError, "workflow failure"):
            with isolated_context(lambda: UpdatingTracer().span(name="stage", metadata={})):
                raise ValueError("workflow failure")

    def test_ordinary_tracer_operation_errors_are_swallowed(self):
        class BrokenUpdate:
            def update(self, **_kwargs):
                raise RuntimeError("tracer update failed")

        class UpdatingTracer:
            @contextmanager
            def span(self, **_kwargs):
                yield BrokenUpdate()

        completed = False
        context = isolated_context(lambda: UpdatingTracer().span(name="stage", metadata={}))
        with context:
            context.update(status="success")
            completed = True
        self.assertTrue(completed)

    def test_tracer_process_interrupts_propagate(self):
        for interrupt_type in (KeyboardInterrupt, SystemExit):
            class InterruptedContext:
                def __enter__(self):
                    raise interrupt_type("stop")

                def __exit__(self, *_args):
                    return False

            with self.subTest(interrupt=interrupt_type.__name__):
                with self.assertRaises(interrupt_type):
                    with isolated_context(lambda: InterruptedContext()):
                        pass

            class InterruptedUpdate:
                def update(self, **_kwargs):
                    raise interrupt_type("stop")

            class InterruptedTracer:
                @contextmanager
                def span(self, **_kwargs):
                    yield InterruptedUpdate()

            with self.subTest(interrupt_update=interrupt_type.__name__):
                with self.assertRaises(interrupt_type):
                    with isolated_context(lambda: InterruptedTracer().span(name="stage", metadata={})):
                        pass

    def test_langfuse_process_interrupts_propagate_from_initialization_and_export(self):
        for interrupt_type in (KeyboardInterrupt, SystemExit):
            tracer = langfuse.BufferedLangfuseTracer(
                session_id="session", public_key="public", secret_key="secret",
                base_url="https://cloud.langfuse.com",
            )
            with tracer.execution(name="workflow.execution", session_id="session", metadata={"command": "start"}):
                pass

            class InterruptedSDK:
                @staticmethod
                def Langfuse(**_kwargs):
                    raise interrupt_type("stop")

            with self.subTest(initialization=interrupt_type.__name__):
                with patch.object(langfuse.importlib, "import_module", return_value=InterruptedSDK()):
                    with self.assertRaises(interrupt_type):
                        tracer.flush()

            class InterruptedClient(FakeClient):
                def start_as_current_observation(self, **_kwargs):
                    raise interrupt_type("stop")

            class ExportSDK:
                @staticmethod
                @contextmanager
                def propagate_attributes(**_kwargs):
                    yield

                @staticmethod
                def Langfuse(**_kwargs):
                    return InterruptedClient()

            with self.subTest(export=interrupt_type.__name__):
                with patch.object(langfuse.importlib, "import_module", return_value=ExportSDK()):
                    with self.assertRaises(interrupt_type):
                        tracer.flush()

    def test_orchestrator_and_review_seam_propagate_process_interrupts(self):
        for interrupt_type in (KeyboardInterrupt, SystemExit):
            runner = object.__new__(orchestrator.Orchestrator)
            runner.state = {
                "authorization": {"observability": {"enabled": True}},
                "stage": "planning",
                "status": "running",
            }
            runner.workflow_id = "interrupt-test"
            runner._tracer = None
            runner._active_trace_context = None
            runner._trace_started_at = None
            with self.subTest(initialization=interrupt_type.__name__):
                with patch("observability.langfuse.create_langfuse_tracer", side_effect=interrupt_type("stop")):
                    with self.assertRaises(interrupt_type):
                        runner._begin_execution_trace("start")

            class InterruptingTracer:
                def flush(self):
                    raise interrupt_type("stop")

            runner._active_trace_context = isolated_context(
                lambda: NoOpTracer().execution(name="workflow.execution", session_id="session", metadata={}),
            )
            runner._active_trace_context.__enter__()
            runner._tracer = InterruptingTracer()
            runner._trace_started_at = 0.0
            with self.subTest(flush=interrupt_type.__name__):
                with self.assertRaises(interrupt_type):
                    runner._finish_execution_trace(None)

            campaign = ReviewCampaign({}, trace_span=lambda *_args: (_ for _ in ()).throw(interrupt_type("stop")))
            with self.subTest(review=interrupt_type.__name__):
                with self.assertRaises(interrupt_type):
                    campaign._trace_context("review.part", {})


if __name__ == "__main__":
    unittest.main()
