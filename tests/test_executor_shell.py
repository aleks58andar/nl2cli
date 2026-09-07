"""Tests for shell execution.

These run real subprocesses for the harmless cases (echo, sleep, false) and
mock the boundary for anything privileged — no test may invoke sudo.
"""

import subprocess
from unittest.mock import MagicMock, patch

import pytest

from src.executor.base import ActionRunner, ExecutionError, Result
from src.executor.shell import (
    ServiceRunner,
    ShellRunner,
    SysVServiceRunner,
    SystemctlRunner,
)
from src.schema import ShellAction

from .conftest import shell


class TestResult:
    def test_success_aliases_ok(self):
        assert Result(ok=True).success is True
        assert Result(ok=False).success is False

    def test_str_of_success(self):
        assert "[SUCCESS]" in str(Result(ok=True))

    def test_str_of_failure_includes_the_code(self):
        assert "[FAILED (127)]" in str(Result(ok=False, error_code=127))

    def test_str_includes_streams_and_change_marker(self):
        text = str(Result(ok=True, stdout="out", stderr="err", changed=True))
        assert "stdout: out" in text
        assert "stderr: err" in text
        assert "(changes made)" in text

    def test_execution_error_carries_the_result(self):
        result = Result(ok=False)
        err = ExecutionError("boom", result)
        assert err.result is result
        assert str(err) == "boom"


class TestShellRunnerExecution:
    def test_successful_command(self):
        result = ShellRunner(shell("echo hello")).run()
        assert result.ok is True
        assert result.stdout.strip() == "hello"
        assert result.error_code == 0
        assert result.changed is True

    def test_failing_command_reports_the_exit_code(self):
        result = ShellRunner(shell("false")).run()
        assert result.ok is False
        assert result.error_code == 1

    def test_missing_binary_is_caught_not_raised(self):
        action = shell("echo hi")
        action.argv = ["definitely-not-a-real-binary-xyz"]
        result = ShellRunner(action).run()
        assert result.ok is False
        assert result.error_code == 1
        assert "Failed to execute command" in result.stderr

    def test_timeout_returns_124(self):
        result = ShellRunner(shell("sleep 5")).run(timeout=1)
        assert result.ok is False
        assert result.error_code == 124
        assert "timed out" in result.stderr

    def test_stderr_is_captured(self):
        action = shell("echo hi")
        action.argv = ["ls", "/definitely/not/here"]
        result = ShellRunner(action).run()
        assert result.ok is False
        assert result.stderr

    def test_command_is_not_interpreted_by_a_shell(self):
        """A glob reaches the binary literally — proof of shell=False."""
        action = shell("echo hi")
        action.argv = ["echo", "*"]
        assert ShellRunner(action).run().stdout.strip() == "*"


class TestOutputTruncation:
    def _proc(self, stdout="", stderr="", returncode=0):
        return subprocess.CompletedProcess(
            args=["x"], returncode=returncode, stdout=stdout, stderr=stderr
        )

    def test_long_stdout_is_truncated(self):
        with patch("subprocess.run", return_value=self._proc(stdout="x" * 20_000)):
            result = ShellRunner(shell("echo hi")).run()
        assert "[...output truncated at 8000 chars]" in result.stdout
        assert len(result.stdout) < 20_000

    def test_long_stderr_is_truncated(self):
        with patch("subprocess.run", return_value=self._proc(stderr="e" * 20_000)):
            result = ShellRunner(shell("echo hi")).run()
        assert "[...stderr truncated at 2000 chars]" in result.stderr

    def test_short_output_is_untouched(self):
        with patch("subprocess.run", return_value=self._proc(stdout="short")):
            assert ShellRunner(shell("echo hi")).run().stdout == "short"

    def test_none_streams_become_empty_strings(self):
        with patch(
            "subprocess.run", return_value=self._proc(stdout=None, stderr=None)
        ):
            result = ShellRunner(shell("echo hi")).run()
        assert result.stdout == ""
        assert result.stderr == ""


class TestSudoRouting:
    """sudo is never in the plan — the runner adds it, or delegates to the manager."""

    def test_sudo_manager_is_preferred_when_present(self):
        manager = MagicMock()
        manager.run_sudo_command.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="", stderr=""
        )
        ShellRunner(shell("systemctl restart ssh", sudo=True), manager).run()
        manager.run_sudo_command.assert_called_once()
        argv = manager.run_sudo_command.call_args[0][0]
        assert argv == ["systemctl", "restart", "ssh"]
        assert "sudo" not in argv

    def test_sudo_is_prepended_when_no_manager_is_available(self):
        with patch("subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="", stderr=""
            )
            ShellRunner(shell("systemctl restart ssh", sudo=True)).run()
        assert run.call_args[0][0] == ["sudo", "systemctl", "restart", "ssh"]

    def test_non_sudo_commands_never_get_sudo(self):
        with patch("subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="", stderr=""
            )
            ShellRunner(shell("ls /etc")).run()
        assert run.call_args[0][0] == ["ls", "/etc"]


class TestPipedCommands:
    def test_read_only_pipe_executes_through_bash_c(self):
        action = shell("echo hello | grep hello")
        assert action.uses_pipe is True
        assert action.argv[:2] == ["bash", "-c"]
        result = ShellRunner(action).run()
        assert result.ok is True
        assert result.stdout.strip() == "hello"

    def test_pipe_execution_still_passes_shell_false(self):
        action = shell("echo a | cat")
        with patch("subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="", stderr=""
            )
            ShellRunner(action).run()
        assert run.call_args.kwargs["shell"] is False


class TestDryRunDescription:
    def test_plain_command(self):
        assert ShellRunner(shell("ls /etc")).dry_run_description() == (
            "Would execute: ls /etc"
        )

    def test_sudo_is_shown_in_the_preview(self):
        text = ShellRunner(shell("systemctl restart ssh", sudo=True)).dry_run_description()
        assert text == "Would execute: sudo systemctl restart ssh"

    def test_arguments_are_shell_quoted(self):
        action = shell("echo hi")
        action.argv = ["echo", "two words"]
        assert "'two words'" in ShellRunner(action).dry_run_description()


class TestFromCommand:
    def test_builds_a_runner_from_a_command_string(self):
        runner = ShellRunner.from_command("say hi", "echo hi")
        assert isinstance(runner, ShellRunner)
        assert runner.description == "say hi"
        assert runner.action.argv == ["echo", "hi"]
        assert runner.requires_sudo is False

    def test_rejects_injection_at_construction(self):
        with pytest.raises(ValueError):
            ShellRunner.from_command("bad", "echo hi; rm -rf /")


class TestSystemctlRunner:
    def test_builds_a_sudo_action(self):
        runner = SystemctlRunner("ssh", "restart")
        assert runner.action.argv == ["systemctl", "restart", "ssh"]
        assert runner.requires_sudo is True

    def test_missing_systemd_fails_cleanly(self):
        with patch("src.executor.shell.run_command") as run_command:
            run_command.return_value = subprocess.CompletedProcess(
                args=[], returncode=1, stdout="", stderr=""
            )
            result = SystemctlRunner("ssh").run()
        assert result.ok is False
        assert "systemd is not available" in result.stderr

    def test_unknown_service_fails_before_execution(self):
        def fake(argv, **kwargs):
            code = 0 if argv[:2] == ["systemctl", "--version"] else 1
            return subprocess.CompletedProcess(args=argv, returncode=code, stdout="")

        with patch("src.executor.shell.run_command", side_effect=fake):
            result = SystemctlRunner("nope").run()
        assert result.ok is False
        assert "not found" in result.stderr

    def test_dot_service_suffix_is_resolved(self):
        def fake(argv, **kwargs):
            if argv[:2] == ["systemctl", "--version"]:
                return subprocess.CompletedProcess(args=argv, returncode=0, stdout="")
            loaded = argv[-1] == "ssh.service"
            return subprocess.CompletedProcess(
                args=argv,
                returncode=0,
                stdout="LoadState=loaded" if loaded else "LoadState=not-found",
            )

        runner = SystemctlRunner("ssh")
        with patch("src.executor.shell.run_command", side_effect=fake), patch(
            "subprocess.run"
        ) as run:
            run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="", stderr=""
            )
            runner.run()
        assert runner.service_name == "ssh.service"
        assert runner.action.argv == ["systemctl", "restart", "ssh.service"]


class TestSysVServiceRunner:
    def test_builds_an_init_script_action(self):
        runner = SysVServiceRunner("ssh", "restart")
        assert runner.action.argv == ["/etc/init.d/ssh", "restart"]
        assert runner.requires_sudo is True

    def test_missing_init_script_fails_cleanly(self):
        result = SysVServiceRunner("definitely-not-a-service").run()
        assert result.ok is False
        assert "not found" in result.stderr


class TestServiceRunner:
    def test_builds_a_service_command_action(self):
        runner = ServiceRunner("nginx", "reload")
        assert runner.action.argv == ["service", "nginx", "reload"]
        assert runner.requires_sudo is True
        assert "Reload nginx" in runner.description


class TestRunnerContract:
    @pytest.mark.parametrize(
        "runner",
        [
            ShellRunner(ShellAction(description="d", command="echo hi")),
            SystemctlRunner("ssh"),
            SysVServiceRunner("ssh"),
            ServiceRunner("ssh"),
        ],
    )
    def test_every_runner_satisfies_the_abc(self, runner):
        assert isinstance(runner, ActionRunner)
        assert isinstance(runner.description, str)
        assert isinstance(runner.requires_sudo, bool)
        assert runner.dry_run_description().startswith("Would execute:")
