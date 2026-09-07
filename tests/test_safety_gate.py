"""Tests for the PoC safety / reliability upgrades.

Covers:
 - Shell operator rejection at the schema level
 - sudo rejection in plan commands
 - Shell-glue (bash -c) rejection in validators
 - Risk gate confirmation logic
 - Per-action timeout abort
"""

import subprocess
import time
from pathlib import Path
from unittest.mock import patch

import pytest
from pydantic import ValidationError

from src.core.schema import ShellAction, EditFileAction, EditMode, Plan, ActionType
from src.validation.risk import compute_risk_score, needs_confirmation
from src.validation.validators import _validate_shell_action
from src.core.schema import HostFacts, InitSystem


# ======================================================================
# 1. Shell operator rejection
# ======================================================================


class TestShellOperatorRejection:
    """ShellAction must reject dangerous shell operators."""

    @pytest.mark.parametrize("cmd", [
        "echo hello; rm -rf /",
        "true && false",
        "true || false",
        "echo hello > /tmp/out",
        "cat < /tmp/in",
        "echo `whoami`",
        "echo $(whoami)",
        "sleep 10 &",
    ])
    def test_dangerous_operators_rejected(self, cmd):
        with pytest.raises(ValidationError):
            ShellAction(description="test", command=cmd)

    @pytest.mark.parametrize("cmd", [
        "ls | grep foo",
        "df -h | grep /dev",
        "find /tmp -type f | sort",
    ])
    def test_readonly_pipes_accepted(self, cmd):
        """Read-only pipes (non-sudo) are allowed."""
        action = ShellAction(description="test", command=cmd)
        assert action.uses_pipe is True
        assert action.argv == ["bash", "-c", cmd]

    def test_pipe_with_sudo_rejected(self):
        with pytest.raises(ValidationError, match="Pipes are not allowed in sudo commands"):
            ShellAction(description="test", command="ls | grep foo", requires_sudo=True)

    def test_pipe_with_write_rejected(self):
        with pytest.raises(ValidationError, match="Pipe with write operation"):
            ShellAction(description="test", command="echo hi | tee /tmp/out")

    @pytest.mark.parametrize("cmd", [
        "ls -la",
        "systemctl restart nginx",
        "apt update",
        "grep pattern file.txt",
        "cp file.txt backup.txt",
        "echo hello world",
    ])
    def test_safe_commands_accepted(self, cmd):
        action = ShellAction(description="test", command=cmd)
        assert action.argv == __import__("shlex").split(cmd)


# ======================================================================
# 2. sudo rejection
# ======================================================================


class TestSudoRejection:
    """sudo must never appear in plan commands."""

    @pytest.mark.parametrize("cmd", [
        "sudo apt update",
        "sudo -u nobody ls",
        "sudo systemctl restart nginx",
    ])
    def test_sudo_in_command_rejected(self, cmd):
        with pytest.raises(ValidationError, match="sudo must not appear"):
            ShellAction(description="test", command=cmd)

    def test_requires_sudo_flag_accepted(self):
        action = ShellAction(
            description="test", command="apt update", requires_sudo=True
        )
        assert action.requires_sudo is True
        assert "sudo" not in action.argv


# ======================================================================
# 3. Shell-glue (bash -c) rejection via validators
# ======================================================================


class TestShellGlueRejection:
    """Explicit shell invocations must be caught by the validator layer."""

    def _host_facts(self):
        return HostFacts(
            distro_id="ubuntu",
            distro_version="22.04",
            init_system=InitSystem.systemd,
            available_binaries=["bash", "sh", "python3"],
            existing_files=[],
            python_version="3.11.0",
        )

    def test_bash_c_rejected(self):
        action = ShellAction(description="test", command="bash -c 'echo hi'")
        issues = _validate_shell_action(action, self._host_facts(), 0)
        errors = [i for i in issues if i.severity == "error"]
        assert any("shell invocation" in i.message.lower() for i in errors)

    def test_sh_c_rejected(self):
        action = ShellAction(description="test", command="sh -c 'echo hi'")
        issues = _validate_shell_action(action, self._host_facts(), 0)
        errors = [i for i in issues if i.severity == "error"]
        assert any("shell invocation" in i.message.lower() for i in errors)

    def test_python_c_rejected(self):
        action = ShellAction(description="test", command="python3 -c 'import os'")
        issues = _validate_shell_action(action, self._host_facts(), 0)
        errors = [i for i in issues if i.severity == "error"]
        assert any("python -c" in i.message.lower() for i in errors)


# ======================================================================
# 4. Risk gate / confirmation logic
# ======================================================================


class TestRiskGate:
    """Risk gate must require confirmation for dangerous plans."""

    def _home(self):
        return str(Path.home())

    def test_sudo_requires_confirmation(self):
        plan = Plan(
            user_request="test",
            actions=[
                ShellAction(
                    description="install pkg",
                    command="apt update",
                    requires_sudo=True,
                ),
            ],
        )
        must, reasons = needs_confirmation(plan, "auto")
        assert must
        assert any("sudo" in r.lower() for r in reasons)

    def test_outside_home_requires_confirmation(self):
        plan = Plan(
            user_request="test",
            actions=[
                EditFileAction(
                    description="edit system config",
                    path="/etc/nginx/nginx.conf",
                    mode=EditMode.replace_line,
                    selector="worker_connections",
                    after="worker_connections 1024;",
                ),
            ],
        )
        must, reasons = needs_confirmation(plan, "auto")
        assert must
        assert any("outside" in r.lower() for r in reasons)

    def test_deletion_requires_confirmation(self):
        plan = Plan(
            user_request="test",
            actions=[
                ShellAction(description="delete tmp", command="rm /tmp/junk"),
            ],
        )
        must, reasons = needs_confirmation(plan, "auto")
        assert must
        assert any("deletion" in r.lower() for r in reasons)

    def test_many_actions_requires_confirmation(self):
        actions = [
            ShellAction(description=f"step {i}", command=f"echo {i}")
            for i in range(5)
        ]
        plan = Plan(user_request="test", actions=actions)
        must, reasons = needs_confirmation(plan, "auto")
        assert must
        assert any(">3" in r for r in reasons)

    def test_safe_plan_no_confirmation(self):
        home = self._home()
        plan = Plan(
            user_request="test",
            actions=[
                ShellAction(description="greet", command="echo hello"),
            ],
        )
        must, _ = needs_confirmation(plan, "auto")
        assert not must

    def test_confirm_mode_always(self):
        plan = Plan(
            user_request="test",
            actions=[ShellAction(description="greet", command="echo hello")],
        )
        must, _ = needs_confirmation(plan, "always")
        assert must

    def test_confirm_mode_never(self):
        plan = Plan(
            user_request="test",
            actions=[
                ShellAction(
                    description="danger",
                    command="apt update",
                    requires_sudo=True,
                ),
            ],
        )
        must, _ = needs_confirmation(plan, "never")
        assert not must

    def test_risk_score_positive_for_sudo(self):
        plan = Plan(
            user_request="test",
            actions=[
                ShellAction(
                    description="install",
                    command="apt update",
                    requires_sudo=True,
                ),
            ],
        )
        assert compute_risk_score(plan) > 0


# ======================================================================
# 5. Timeout aborts execution
# ======================================================================


class TestTimeout:
    """Per-action timeout must abort and return an error result."""

    def test_timeout_returns_error(self):
        action = ShellAction(description="slow", command="sleep 60")
        from src.execution.shell import ShellRunner

        def _fake_run(argv, **kwargs):
            raise subprocess.TimeoutExpired(cmd=argv, timeout=kwargs.get("timeout", 1))

        runner = ShellRunner(action)
        with patch("subprocess.run", side_effect=_fake_run):
            result = runner.run(timeout=1)
        assert not result.ok
        assert result.error_code == 124
        assert "timed out" in result.stderr.lower()


# ======================================================================
# 6. No shell=True anywhere
# ======================================================================


class TestNoShellTrue:
    """Verify that no execution path uses shell=True."""

    def test_run_command_uses_argv(self):
        from src.core.utils import run_command

        result = run_command(["echo", "hello"], timeout=5)
        assert result.returncode == 0
        assert "hello" in result.stdout

    def test_sudo_manager_uses_argv(self):
        from src.execution.sudo_manager import SudoManager
        import inspect

        src = inspect.getsource(SudoManager.run_sudo_command)
        assert "shell=True" not in src
        assert "shell = True" not in src
