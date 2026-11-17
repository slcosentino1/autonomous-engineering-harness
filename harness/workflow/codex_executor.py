"""Codex CLI configuration and subprocess execution boundary."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import time
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Literal, Mapping

from tools.read_documentation_url import authorized_url_sha256
from workflow import process_control as _process_control
from workflow import workspace as _workspace
from workflow.codex_evidence import _output_bytes


class CodexExecutionConfigurationError(ValueError):
    """A Codex CLI configuration cannot be prepared safely."""


class CodexLaunchError(OSError):
    """Codex could not start before a process was created."""

    def __init__(self, message: str, *, error_category: str):
        super().__init__(message)
        self.error_category = error_category


class CodexExecutionUncertainError(RuntimeError):
    """A process may have started, but its terminal result was not trustworthy."""

    def __init__(self, message: str, *, error_category: str):
        super().__init__(message)
        self.error_category = error_category


ExecutionOutcome = Literal[
    "completed", "launch_failed", "nonzero_exit", "timed_out", "cancelled", "result_unavailable",
]


@dataclass(frozen=True)
class CodexExecutionRequest:
    """Inputs required to execute one already-authorized Codex operation."""

    operation_id: str
    command: tuple[str, ...]
    prompt_bytes: bytes = field(repr=False)
    working_directory: Path
    timeout_seconds: float
    environment: Mapping[str, str] = field(repr=False)


@dataclass(frozen=True)
class CodexExecutionResult:
    """Observed process outcome; it carries no workflow policy or state mutations."""

    operation_id: str
    outcome: ExecutionOutcome
    exit_code: int | None
    stdout: bytes | str | None = field(repr=False)
    stderr: bytes | str | None = field(repr=False)
    duration_ms: int
    process_id: int | None
    error_category: str | None = None
    error_message: str | None = field(default=None, repr=False)


def build_codex_command(
    executable: str,
    target: Path,
    sandbox: str,
    output_path: Path,
    model: str | None = None,
    *,
    read_only_authorized_urls: list[str] | None = None,
    disabled_user_mcp_servers: list[str] | None = None,
) -> list[str]:
    """Build the installed CLI's supported command; approval policy is a root option."""
    command = [executable]
    authorized_urls = sorted(set(read_only_authorized_urls or [])) if sandbox == "read-only" else []
    command.extend(["--ask-for-approval", "never", "exec"])
    command.extend([
        "--json", "--ephemeral", "--disable", "plugins",
        "--sandbox", sandbox, "--cd", str(target),
        "--output-last-message", str(output_path),
    ])
    user_mcp_servers: list[str] = []
    if sandbox == "read-only":
        if disabled_user_mcp_servers is None:
            raise CodexExecutionConfigurationError("Read-only Codex calls require explicit user MCP isolation.")
        if any(not isinstance(name, str) or not name for name in disabled_user_mcp_servers):
            raise CodexExecutionConfigurationError(
                "Configured Codex MCP server names are invalid; planning stopped before provider execution."
            )
        user_mcp_servers = sorted(set(disabled_user_mcp_servers))
    if authorized_urls and "documentation_reader" in user_mcp_servers:
        raise CodexExecutionConfigurationError(
            "Read-only documentation access cannot use the configured MCP name 'documentation_reader'."
        )
    mcp_entries = [f"{json.dumps(name)} = {{ enabled = false }}" for name in user_mcp_servers]
    if authorized_urls:
        docs_args = [
            str(Path(__file__).resolve().parents[1] / "tools" / "read_documentation_url.py"),
            *[
                item
                for url in authorized_urls
                for item in ("--authorized-url-sha256", authorized_url_sha256(url))
            ],
        ]
        mcp_entries.append(
            f'"documentation_reader" = {{ enabled = true, command = {json.dumps(sys.executable)}, '
            f"args = {json.dumps(docs_args)} }}"
        )
    mcp_config = "mcp_servers={ " + ", ".join(mcp_entries) + " }" if mcp_entries else "mcp_servers={}"
    command.extend(["-c", mcp_config])
    if model is not None:
        command.extend(["--model", model])
    command.append("-")
    return command


def configured_codex_mcp_servers(environ: Mapping[str, str] | None = None) -> list[str]:
    """Return user-configured MCP names without exposing provider configuration values."""
    env = os.environ if environ is None else environ
    configured_home = env.get("CODEX_HOME")
    codex_home = Path(configured_home).expanduser() if configured_home else Path.home() / ".codex"
    config_path = codex_home / "config.toml"
    try:
        config_text = config_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    except (OSError, UnicodeError) as exc:
        raise CodexExecutionConfigurationError(
            "Could not inspect Codex user configuration to isolate MCP servers; planning stopped before provider execution."
        ) from exc
    try:
        config = tomllib.loads(config_text)
    except tomllib.TOMLDecodeError as exc:
        raise CodexExecutionConfigurationError(
            "Codex user configuration is invalid; planning stopped before provider execution to preserve MCP isolation."
        ) from exc
    servers = config.get("mcp_servers", {})
    if not isinstance(servers, dict) or any(not isinstance(name, str) or not name for name in servers):
        raise CodexExecutionConfigurationError(
            "Codex user MCP configuration is invalid; planning stopped before provider execution."
        )
    return sorted(servers)


def prepare_provider_environment(isolated_config_directory: Path) -> dict[str, str]:
    """Prepare the sanitized Codex subprocess environment for one invocation."""
    return _workspace.provider_env(isolated_config_directory)


def read_cli_version(
    executable: str,
    working_directory: Path,
    *,
    process_runner: Callable = subprocess.run,
) -> str | None:
    """Read optional CLI version metadata without making a provider request."""
    if process_runner is not subprocess.run:
        return None
    try:
        with tempfile.TemporaryDirectory(prefix="harness-codex-version-") as isolated_config:
            result = subprocess.run(
                [executable, "--version"],
                cwd=str(working_directory),
                env=prepare_provider_environment(Path(isolated_config)),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=3,
                check=False,
            )
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    if result.returncode != 0:
        return None
    version_bytes, _source = _output_bytes(result.stdout)
    if version_bytes is None:
        return None
    version = version_bytes.decode("utf-8", "replace").strip().splitlines()
    if not version or len(version[0]) > 96:
        return None
    if re.fullmatch(r"(?:codex-cli|codex)\s+v?[0-9][A-Za-z0-9.+-]*", version[0], re.IGNORECASE):
        return version[0]
    return None


class CodexExecutor:
    """Execute one explicit Codex request without access to workflow state."""

    def __init__(self, *, process_runner: Callable = subprocess.run):
        self._process_runner = process_runner

    def execute(
        self,
        request: CodexExecutionRequest,
        *,
        on_process_started: Callable[[int], None],
    ) -> CodexExecutionResult:
        started = time.monotonic()
        process_id: int | None = None
        command = list(request.command)
        if self._process_runner is subprocess.run:
            try:
                process = subprocess.Popen(
                    command,
                    stdin=subprocess.PIPE,
                    **_process_control.process_group_options(),
                    cwd=str(request.working_directory),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    env=request.environment,
                )
            except OSError as exc:
                return self._result(
                    request, "launch_failed", started, error_category=type(exc).__name__,
                    error_message=str(exc),
                )
            process_id = process.pid
            try:
                on_process_started(process_id)
            except KeyboardInterrupt:
                try:
                    self._terminate_and_reap(process)
                except BaseException as exc:
                    return self._result(
                        request, "result_unavailable", started, process_id=process_id,
                        error_category=type(exc).__name__, error_message=str(exc),
                    )
                return self._result(request, "cancelled", started, process_id=process_id)
            except BaseException as exc:
                try:
                    self._terminate_and_reap(process)
                except BaseException as cleanup_error:
                    return self._result(
                        request, "result_unavailable", started, process_id=process_id,
                        error_category=type(cleanup_error).__name__,
                        error_message=f"{exc}; process cleanup failed: {cleanup_error}",
                    )
                return self._result(
                    request, "result_unavailable", started, process_id=process_id,
                    error_category=type(exc).__name__, error_message=str(exc),
                )
            try:
                stdout, stderr = process.communicate(input=request.prompt_bytes, timeout=request.timeout_seconds)
            except subprocess.TimeoutExpired:
                try:
                    stdout, stderr = self._terminate_and_reap(process)
                except BaseException as exc:
                    return self._result(
                        request, "result_unavailable", started, process_id=process_id,
                        error_category=type(exc).__name__, error_message=str(exc),
                    )
                return self._result(
                    request, "timed_out", started, stdout=stdout, stderr=stderr,
                    process_id=process_id,
                )
            except KeyboardInterrupt:
                try:
                    self._terminate_and_reap(process)
                except BaseException as exc:
                    return self._result(
                        request, "result_unavailable", started, process_id=process_id,
                        error_category=type(exc).__name__, error_message=str(exc),
                    )
                return self._result(request, "cancelled", started, process_id=process_id)
            except BaseException as exc:
                try:
                    self._terminate_and_reap(process)
                except BaseException as cleanup_error:
                    return self._result(
                        request, "result_unavailable", started, process_id=process_id,
                        error_category=type(cleanup_error).__name__,
                        error_message=f"{exc}; process cleanup failed: {cleanup_error}",
                    )
                return self._result(
                    request, "result_unavailable", started, process_id=process_id,
                    error_category=type(exc).__name__, error_message=str(exc),
                )
            return self._result(
                request,
                "completed" if process.returncode == 0 else "nonzero_exit",
                started,
                exit_code=process.returncode,
                stdout=stdout,
                stderr=stderr,
                process_id=process_id,
            )

        try:
            process_result = self._process_runner(
                command,
                input=request.prompt_bytes,
                timeout=request.timeout_seconds,
                check=False,
                cwd=str(request.working_directory),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=request.environment,
            )
        except subprocess.TimeoutExpired as exc:
            return self._result(
                request, "timed_out", started, stdout=exc.output, stderr=exc.stderr,
            )
        except KeyboardInterrupt:
            return self._result(request, "cancelled", started)
        except OSError as exc:
            return self._result(
                request, "launch_failed", started,
                error_category=type(exc).__name__, error_message=str(exc),
            )
        except BaseException as exc:
            return self._result(
                request, "result_unavailable", started,
                error_category=type(exc).__name__, error_message=str(exc),
            )
        candidate_pid = getattr(process_result, "pid", None)
        if isinstance(candidate_pid, int) and not isinstance(candidate_pid, bool) and candidate_pid > 0:
            process_id = candidate_pid
            try:
                on_process_started(process_id)
            except KeyboardInterrupt:
                return self._result(request, "cancelled", started, process_id=process_id)
            except BaseException as exc:
                return self._result(
                    request, "result_unavailable", started, process_id=process_id,
                    error_category=type(exc).__name__, error_message=str(exc),
                )
        return_code = getattr(process_result, "returncode", None)
        if not isinstance(return_code, int) or isinstance(return_code, bool):
            return self._result(
                request, "result_unavailable", started, process_id=process_id,
                error_category="InvalidProcessResult", error_message="Codex process result has no valid exit code.",
            )
        return self._result(
            request,
            "completed" if return_code == 0 else "nonzero_exit",
            started,
            exit_code=return_code,
            stdout=getattr(process_result, "stdout", None),
            stderr=getattr(process_result, "stderr", None),
            process_id=process_id,
        )

    @staticmethod
    def _terminate_and_reap(process: subprocess.Popen) -> tuple[object, object]:
        _process_control.terminate_process_group(process)
        return process.communicate()

    @staticmethod
    def _result(
        request: CodexExecutionRequest,
        outcome: ExecutionOutcome,
        started: float,
        *,
        exit_code: int | None = None,
        stdout: bytes | str | None = None,
        stderr: bytes | str | None = None,
        process_id: int | None = None,
        error_category: str | None = None,
        error_message: str | None = None,
    ) -> CodexExecutionResult:
        return CodexExecutionResult(
            operation_id=request.operation_id,
            outcome=outcome,
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            duration_ms=max(0, round((time.monotonic() - started) * 1000)),
            process_id=process_id,
            error_category=error_category,
            error_message=error_message,
        )
